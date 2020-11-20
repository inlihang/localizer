# Copyright 2018-2020 Ivan Alles. See also the LICENSE file.

"""
Terms:
    image CS - image coordinate system.
    input CS - the CS of a training example, which is a randomly placed and rotated image patch.
    target CS - the CS of the target tensors.
"""

import datetime
import io
import json
import logging
from multiprocessing import Pool
import os
import sys

import cv2
import numpy as np
import tensorflow as tf
from tensorflow import keras

from robogym import geometry
from robogym import random

from localizer import dataset
from localizer import predict
from localizer import utils

logger = logging.getLogger(__name__)

g_batch_gen = None

TRAINING_EXAMPLES_DIAG_DIR_NAME = 'training_examples'

INFINITE_DIFF = np.full(2, np.inf)

CATEGORY_ALL = 'ALL'


class Batch:
    """
    A batch. Will be filled by tensors for training.
    """

    def __init__(self, size, input_shape, output_shape):
        self.input = np.empty(shape=(size,) + input_shape, dtype=np.float32)
        self.target_window = np.zeros(shape=(size,) + output_shape, dtype=np.float32)
        self.weight = np.empty(shape=(size,) + output_shape[:3] + (1,), dtype=np.float32)


def _worker_process_init(batch_gen):
    # Store batch_gen in a global variable for future reference
    global g_batch_gen
    g_batch_gen = batch_gen
    g_batch_gen.init_after_pickle()


def _worker_process_generate_batch(generate_batch_args):
    result = g_batch_gen.generate_batch(**generate_batch_args)
    return result


class DataElementFilter:
    """
    Used to select subsets from the set of data elements, e.g. train, validation, test.
    """
    def __init__(self, filter_id, hash_range=None):
        """
        Create new filter.
        :param filter_id: filter id.
        :param hash_range: a tuple to define range [min, max). None: skip this check.
        """
        self.id = filter_id
        self.hash_range = hash_range
        self.data_element_indices = []  # Will be populated in add_data_element()

        # A numpy 1d array of the same size as self.data_element_indices
        # containing their weights. Must be populated by the user.
        self.data_element_weights = None

    @property
    def size(self):
        return len(self.data_element_indices)

    def add_data_element(self, data_element_index, hash_string=None):
        """
        Check if data element matches the filter. Name and hast_string matches are ANDed together.
        If matches, add it to the filter.
        :param data_element_index: data element index.
        :param hash_string: hash string, e.g. marker coordinates as text. None: skip this check.
        :return: True if the data element has been added to the filter.
        """
        is_accepted = True
        if hash_string is not None and self.hash_range is not None:
            hash_value = random.str_to_random(hash_string)
            is_accepted = self.hash_range[0] <= hash_value < self.hash_range[1]

        if is_accepted:
            self.data_element_indices.append(data_element_index)

        return is_accepted


class BatchGenerator:
    """
    Generates batches of train and validation data. Executes batch generation jobs,
    managed by a MultiProcessBatchGenerator object.

    An master instance of the class is passed to MultiProcessBatchGenerator object. The latter
    will start multiple processes and pickle the master instance into them.

    It randomly selects data elements indices from filters, e.g. train, validation, all.
    """
    def __init__(self, cfg, data_elements, filters):
        """
        Create new object (master instance). All data created here must be picklable.
        Extend in a derived class to populate data elements and filters.

        :param filters: a list of DataElementFilter instances.
        """
        self._data_elements = data_elements
        self._cfg = cfg
        self._rng_seed = cfg['rng_seed']
        # Initialize for the master instance.
        self._rng = np.random.RandomState(self._rng_seed)
        self._filters = {f.id: f for f in filters}
        if len(self._filters) != len(filters):
            raise ValueError('Filter id duplications found.')

    def init_after_pickle(self):
        """
        2nd step of the initialization done after passing this object into a new process.
        """
        # Initialize rng with a distinct value for each process.
        self._rng = np.random.RandomState(os.getpid() + self._rng_seed)

    def generate_batch(self, batch_size, filter_id):
        """
        Generate a batch.
        :return a batch object (a dictionary or an instance of a user-defined class)
            containing data in picklable format (e.g. numpy arrays).
        """
        flt = self._filters[filter_id]
        assert flt.data_element_weights is not None, 'Have you forgotten to initialize the probability array?'
        data_element_indices = self._rng.choice(flt.data_element_indices,
                                                size=batch_size,
                                                p=flt.data_element_weights)

        if len(data_element_indices) == 0:
            return None

        batch = Batch(len(data_element_indices),
                      tuple(self._cfg['input_shape']),
                      self._cfg['runtime']['output_shape'])
        for i, data_element_idx in enumerate(data_element_indices):
            self._data_elements[data_element_idx].make_training_data(batch, i, self._rng)

        return batch


class MultiProcessBatchGenerator:
    """
    A batch generator using multiple processes.

    Manages instances of subclasses of BatchGenerator running in multiple processes.
    Batches are generated in parallel and in advance to utilize multi-core CPUs.
    """

    def __init__(self, master_batch_gen, process_count=None, max_jobs_count=10):
        """
        Creates a new instance.

        :param master_batch_gen: user-defined class derived from BatchGenerator.
        :param process_count: number of processes to create.
               None - use default number of processes. This is the number of CPUs including hyperthreading.
               0 - run in the main process, useful for debugging.
               > 0 - use this number of processes.
               Under debugger this parameter is always replaced by 0.
        :param max_jobs_count: the maximal number of jobs queued for parallel execution.
        """

        self._master_batch_gen = master_batch_gen

        # Disable multiprocessing under debugger (either does not work or difficult to debug).
        is_under_debugger = sys.gettrace() is not None
        process_count = 0 if is_under_debugger else process_count

        self._process_count = process_count
        self._max_jobs_count = max_jobs_count
        self._current_training_examples_count = 0
        self._generate_batch_args = None

        if self._process_count != 0:
            # Pass the initialized batch generator to the pool thread once and for all now
            # to avoid copying its data in every job.
            self._pool = Pool(processes=self._process_count, initializer=_worker_process_init,
                              initargs=[self._master_batch_gen])
            self._jobs = []

        super().__init__()

    def start_generation(self, batch_size, filter_id):
        """
        Must be called before generation a series of batches. If previous generation is running, stops it first.
        :param batch_size required number of training examples in a batch.
        :param filter_id: filter id to select samples from.
        """
        self.stop_generation()
        self._generate_batch_args = {
            'batch_size': batch_size,
            'filter_id': filter_id,
        }

    def stop_generation(self):
        """
        Wait for completion of all running jobs.
        """
        if self._process_count == 0:
            return
        while len(self._jobs) > 0:
            self._wait_for_ready_job()

    def generate_batch(self):
        """
        Generate a batch.
        :return: a batch object. See also BatchGenerator.generate_batch().
        """

        if self._process_count == 0:
            batch = self._master_batch_gen.generate_batch(**self._generate_batch_args)
        else:
            # Make sure the thread pool is loaded with jobs in advance.
            self._add_new_jobs()

            batch = self._wait_for_ready_job()
            # Compensate for the removed job.
            self._add_new_jobs()

        return batch

    def close(self):
        """
        Will stop the worker processes forever. The object cannot be used anymore.
        """
        if self._process_count == 0:
            return
        self.stop_generation()
        if self._process_count != 0:
            self._pool.close()
            self._pool.join()

        self._master_batch_gen = None

    def _wait_for_ready_job(self):
        """
        Wait for any job to be ready and remove it from the job list.
        """
        if len(self._jobs) == 0:
            raise Exception('No jobs')
        timeout = 0  # In first iteration quickly scan all jobs
        while True:
            for j in range(len(self._jobs)):
                job = self._jobs[j]
                if timeout > 0:
                    job.wait(timeout=timeout)
                if job.ready():
                    del self._jobs[j]
                    batch = job.get()
                    return batch
            timeout = 0.050  # Now wait for some time.

    def _add_new_jobs(self):
        new_jobs_count = max(0, self._max_jobs_count - len(self._jobs))

        for b in range(new_jobs_count):
            job = self._pool.apply_async(_worker_process_generate_batch, (self._generate_batch_args,))
            self._jobs.append(job)


class CategoryStatistics:
    """
    Collects validation data for one category.
    """

    def __init__(self, category):
        self.category = category
        self.total = 0
        self.found = 0  # Both pose and category match.
        # Accumulates (ground_truth - pred)^2 for found objects.
        self.rmse_position = 0  # In pixels.
        self.rmse_orientation = 0  # In degrees.
        self.max_err_position = 0  # In pixels.
        self.max_err_orientation = 0  # In degrees.
        self.misclassified = {}  # Pose matches but category is wrong.
        self.missing = 0  # No matching predicted object found.
        self.ufo = 0  # Predicted object is unmatched to any ground truth objects.

    def get_header(self, categories):
        header = ' Cat|  Total|  Found|     %|RMSE pos|RMSE or|Max  pos|Max  or|'
        for c in categories:
            header += 'MCl{0:4}|     %|'.format(c)
        header += 'MCl ALL|     %|'
        header += 'Missing|     %|    UFO|     %'
        return header

    def get_text(self, categories):
        total_pc = 100 / self.total if self.total > 0 else 0
        text = '{0:4}|{1:7}|{2:7}|{3:6.2f}|{4:8.2f}|{5:7.2f}|{6:8.2f}|{7:7.2f}|'.format(
            self.category,
            self.total,
            self.found,
            self.found * total_pc,
            self.rmse_position,
            self.rmse_orientation,
            self.max_err_position,
            self.max_err_orientation
        )

        for c in categories:
            if c == self.category:
                text += '-------|------|'
            else:
                text += '{0:7}|{1:6.2f}|'.format(
                    self.misclassified.get(c, 0),
                    self.misclassified.get(c, 0) * total_pc)

        total_misclassified = 0
        for k, v in self.misclassified.items():
            total_misclassified += v

        text += '{0:7}|{1:6.2f}|'.format(
            total_misclassified,
            total_misclassified * total_pc)

        object_count = self.found + sum(self.misclassified.values()) + self.ufo

        text += '{0:7}|{1:6.2f}|{2:7}|{3:6.2f}'.format(
            self.missing,
            self.missing * total_pc,
            self.ufo,
            self.ufo * 100 / object_count if object_count > 0 else 0)

        return text

    def add(self, other):
        self.total += other.total
        self.found += other.found
        self.rmse_position += other.rmse_position
        self.rmse_orientation += other.rmse_orientation
        self.max_err_position = max(self.max_err_position, other.max_err_position)
        self.max_err_orientation = max(self.max_err_orientation, other.max_err_orientation)
        for k, v in other.misclassified.items():
            self.misclassified[k] = self.misclassified.get(k, 0) + v
        self.missing += other.missing
        self.ufo += other.ufo

    def finalize(self):
        if self.found > 0:
            self.rmse_position = np.sqrt(self.rmse_position / self.found)
            self.rmse_orientation = np.sqrt(self.rmse_orientation / self.found)


class Trainer:
    """
    Trains and validates a model.
    """

    def __init__(self, config_path):
        self._model_dir = os.path.dirname(config_path)
        with open(config_path, encoding='utf-8') as f:
            cfg = json.load(f)
        self._cfg = cfg
        # The config will be augmented by some parameters and objects in run-time
        # (one object configures another).
        self._cfg['runtime'] = {}
        self._output_dir = os.path.join(self._model_dir, '.temp/train')
        self._cfg['runtime']['output_dir'] = self._output_dir

    def run(self):
        """
        The main function.
        """
        utils.make_clean_directory(self._output_dir)
        self._read_dataset()
        self._train()
        print('Done.')

    @property
    def _category_count(self):
        """
        A shortcut for the number of categories.
        """
        return self._cfg['runtime']['category_count']

    def _read_dataset(self):
        """
        Read the dataset and assign weights for data elements.
        """

        validation_fraction = self._cfg['validation_fraction']
        self._filters = {
            flt.id: flt for flt in
            [
                DataElementFilter('validate', hash_range=(0, validation_fraction)),
                DataElementFilter('train', hash_range=(validation_fraction, 1)),
                DataElementFilter('all')
            ]
        }

        self._dataset = dataset.Dataset(self._cfg['dataset'], self._cfg)

        accepted_data_element_indices = []
        categories = set()

        for i, data_element in enumerate(self._dataset.data_elements):
            is_accepted = False
            for flt in self._filters.values():
                distinct_name = data_element.path
                if flt.add_data_element(i, hash_string=distinct_name):
                    is_accepted = True
            if not is_accepted:
                continue
            accepted_data_element_indices.append(i)
            for obj in data_element.objects:
                categories.add(obj.category)

        self._cfg['runtime']['category_count'] = len(categories)

        self._dataset.precompute_training_data(accepted_data_element_indices)

        for flt_name, flt in self._filters.items():
            print(f'Filter "{flt.id}" contains {flt.size} data elements')
            if flt.size == 0:
                continue
            # Assign probabilities to ensure category balance
            total_obj_count_by_cat = {k: 0 for k in range(self._category_count)}
            for data_element_idx in flt.data_element_indices:
                for obj in self._dataset.data_elements[data_element_idx].objects:
                    total_obj_count_by_cat[obj.category] += 1

            flt.data_element_weights = np.zeros(flt.size, dtype=np.float64)

            for i, data_element_idx in enumerate(flt.data_element_indices):
                for obj in self._dataset.data_elements[data_element_idx].objects:
                    flt.data_element_weights[i] += 1 / total_obj_count_by_cat[obj.category] / self._category_count

            assert np.allclose(1, flt.data_element_weights.sum()), flt_name

    def _make_model(self):
        """
        Creates a model and loss.
        :return:
        """
        input_shape = tuple(self._cfg['input_shape'])
        self._sigma = float(self._cfg['sigma'])
        self._image_input = keras.Input(shape=[None, None, input_shape[2]], dtype='float32', name='image')

        print(f"Creating model, input shape {input_shape}")
        self._create_model()
        tf.keras.utils.plot_model(self._model, to_file=self._output_dir + '/model.svg', dpi=50, show_shapes=True)

        # Run a model once to find out its output shape for the training input shape.
        dummy_input = np.expand_dims(np.zeros(self._cfg['input_shape']), 0)
        self._output_shape = self._model.predict(dummy_input).shape[1:]
        self._cfg['runtime']['output_shape'] = self._output_shape
        print(f'Output shape: {self._model.output.shape}, parameters: {self._model.count_params()}')

        self._target_window_input = keras.Input(shape=self._model.output.shape[1:], dtype='float32',
                                                name='target_window')
        self._weight_input = keras.Input(shape=self._model.output.shape[1:-1] + (1,),
                                         dtype='float32', name='weight')

        self._create_loss()

    def _create_model(self):
        """
        Creates a new model (self._model).
        """

        # Create feature extractor
        f = self._image_input - tf.constant(self._dataset.image_mean, name='image_mean')
        feature_sizes = [16, 32, 64]
        conv_params = {'activation': 'relu', 'padding': 'same'}
        pool_params = {'pool_size': (2, 2), 'pool_size': (2, 2), 'padding': 'valid'}
        f = keras.layers.Conv2D(feature_sizes[0], (3, 3), **conv_params)(f)
        f = keras.layers.Conv2D(feature_sizes[0], (3, 3), **conv_params)(f)
        f = keras.layers.AveragePooling2D(**pool_params)(f)
        f = keras.layers.Conv2D(feature_sizes[1], (3, 3), **conv_params)(f)
        f = keras.layers.Conv2D(feature_sizes[1], (3, 3), **conv_params)(f)
        f = keras.layers.AveragePooling2D(**pool_params)(f)
        f = keras.layers.Conv2D(feature_sizes[2], (3, 3), **conv_params)(f)
        f = keras.layers.Conv2D(feature_sizes[2], (3, 3), **conv_params)(f)
        f = keras.layers.AveragePooling2D(**pool_params)(f)

        tf.keras.utils.plot_model(keras.Model(inputs=self._image_input, outputs=f),
                                  to_file=self._output_dir + '/features.svg', dpi=50, show_shapes=True)

        category_models = []

        for cat in range(self._category_count):
            category_model = keras.layers.Dropout(0.3)(f)
            category_model = keras.layers.Conv2D(32, (11, 11), **conv_params)(category_model)
            category_model = keras.layers.Dropout(0.3)(category_model)
            conv_params['activation'] = None
            category_model = keras.layers.Conv2D(predict.TrainingModelChannels.COUNT, (1, 1),
                                                 **conv_params, name=f'category_model{cat}')(category_model)
            category_models.append(category_model)

        category_models = tf.stack(category_models, axis=1)

        self._model = keras.Model(inputs=self._image_input, outputs=category_models, name='model')

    def _create_loss(self):
        """
        Creates a loss function.
        """
        def make_window(v, name=''):
            """
            Compute window function:

            gaussian = exp(-0.5 * (x**2 + y**2) / sigma**2)

            wx = x / sigma * gaussian
            wy = y / sigma * gaussian
            wsa = sin(a) * gaussian
            wca = cos(a) * gaussian
            """
            # Remove angles
            xy = v * [1. / self._sigma, 1. / self._sigma, 0, 0]
            # Compute (x**2 + y**2) / sigma**2
            sr2 = tf.reduce_sum(tf.square(xy), axis=4, keepdims=True)
            gaussian = tf.math.exp(-0.5 * sr2)
            window = v * [1. / self._sigma, 1. / self._sigma, 1, 1] * gaussian
            return window

        output_window = make_window(self._model.output, 'output_window')

        w_loss = np.array(self._cfg['w_loss'], dtype=np.float32)
        w_loss /= w_loss.sum()
        loss = w_loss * tf.square(output_window - self._target_window_input)

        weight = self._weight_input

        loss = loss * weight
        # Normalize to make loss comparable for different output sizes and number of categories.
        loss = tf.reduce_sum(loss) / (tf.reduce_sum(weight) + 1e-5)

        self._loss = keras.Model(inputs=(self._image_input, self._target_window_input, self._weight_input),
                                 outputs=(loss, self._model.output), name='loss')
        tf.keras.utils.plot_model(self._loss, to_file=self._output_dir + '/loss.svg', dpi=50, show_shapes=True)

        self._diag_window_image_input = keras.Input(self._model.output.shape[1:], dtype='float32')
        diag_window_func = make_window(self._diag_window_image_input)
        self._diag_window_func = keras.Model(inputs=self._diag_window_image_input, outputs=diag_window_func)
        tf.keras.utils.plot_model(self._diag_window_func, to_file=self._output_dir + '/diag_window_func.svg',
                                  dpi=50, show_shapes=True)

    def _train(self):
        """
        Train a model.
        """
        train_phase_params = [
            {
                'name': 'validated',
                'train_filter_name': 'train',
                'validate_filter_name': 'validate',
            },
            {
                'name': 'final',
                'train_filter_name': 'all',
                'validate_filter_name': 'all',
            }
        ]

        # Test code to validate without training.
        # self._validate_model(train_phase_params[1], True)
        # return

        total_training_examples_count = 0
        for p in train_phase_params:
            total_training_examples_count += self._cfg[p['name'] + '_training_examples_count']

        if total_training_examples_count == 0:
            return  # Nothing to do

        self._make_model()
        self._batchgen = MultiProcessBatchGenerator(
            BatchGenerator(self._cfg, self._dataset.data_elements, self._filters.values()),
            self._cfg['process_count'])

        self._optimizer = keras.optimizers.Adam(learning_rate=0.0005)

        for p in train_phase_params:
            total_training_examples_count += self._cfg[p['name'] + '_training_examples_count']
            self._train_phase(p)

        self._batchgen.close()

        print('Training done')

    def _train_phase(self, train_phase_params):
        train_filter_name = train_phase_params['train_filter_name']
        validate_filter_name = train_phase_params['validate_filter_name']

        print(f'----------------------- {train_phase_params["name"]} training -------------------------------------')
        print(f'Train on {self._filters[train_filter_name].size} data elements.')
        print(f'Validate on {self._filters[validate_filter_name].size} data elements.')

        training_examples_to_make = self._cfg[train_phase_params['name'] + '_training_examples_count']
        training_examples_done = 0

        def is_phase_finished():
            return training_examples_done >= training_examples_to_make

        # Loop over epochs
        while not is_phase_finished():
            epoch_start_time = datetime.datetime.now()
            self._batchgen.start_generation(self._cfg['training_examples_per_batch'], train_filter_name)
            training_examples_in_epoch_done = 0
            self._epoch_loss = tf.keras.metrics.Mean()
            # Loop over batches in epoch
            while not is_phase_finished() and \
                    training_examples_in_epoch_done < self._cfg['training_examples_per_epoch']:
                batch = self._batchgen.generate_batch()
                training_examples_done += len(batch.input)
                training_examples_in_epoch_done += len(batch.input)

                arguments = (batch.input, batch.target_window, batch.weight)
                with tf.GradientTape() as tape:
                    loss_value, model_output = self._loss(arguments, training=True)
                grads = tape.gradient(loss_value, self._loss.trainable_variables)
                self._optimizer.apply_gradients(zip(grads, self._loss.trainable_variables))
                self._epoch_loss(loss_value)

                batch.output = model_output

                # Update diagnostics on the 1st batch in epoch.
                if training_examples_in_epoch_done == len(batch.input):
                    batch.output_window = self._diag_window_func(batch.output)
                    te_dir = os.path.join(self._output_dir, TRAINING_EXAMPLES_DIAG_DIR_NAME)
                    utils.save_batch_as_images(batch, te_dir, fmt='{1:0>3}{2}.png')

            epoch_run_time = (datetime.datetime.now() - epoch_start_time).total_seconds()
            te_per_sec = training_examples_in_epoch_done / epoch_run_time
            print(f'Training examples: {training_examples_done}, loss: {self._epoch_loss.result():.6f}, '
                  f'{te_per_sec:.2f} training examples/s.')

            self._model.save(os.path.join(self._model_dir, 'model.tf'))
            self._validate_model(train_phase_params,
                                 train_phase_params['name'] == 'final' and is_phase_finished())

    def _validate_model(self, train_phase_params, extended_validation):
        localizer = predict.Localizer(self._model_dir)
        localizer.diag = False  # Set to true to see diagnostic images
        localizer_diag_dir = os.path.join(self._output_dir, 'localizer_diag')

        result_dir = os.path.join(self._output_dir, 'validate')
        utils.make_clean_directory(result_dir)

        self._summary_log = io.StringIO()
        self._details_log = io.StringIO()

        image_count = 0
        gt_stats = {}

        try:
            if localizer.diag:
                utils.make_clean_directory(localizer_diag_dir)
        except OSError:
            pass

        run_times = []
        validate_filter = self._filters[train_phase_params['validate_filter_name']]
        for i in validate_filter.data_element_indices:
            data_element = self._dataset.data_elements[i]
            image_path = data_element.path
            image_file = os.path.basename(image_path)
            image_count += 1
            localizer.diag_dir = os.path.join(localizer_diag_dir, image_file)

            start_time = datetime.datetime.now()
            image = data_element.read_image()
            pr_objects = localizer.predict(image)
            run_times.append((datetime.datetime.now() - start_time).total_seconds())

            gt_objects = data_element.objects
            self._log(f'Image file {image_file}, ground truth objects: {len(gt_objects)}, '
                      f'predicted objects {len(pr_objects)}', 'd')

            self._cross_match_objects(gt_objects, pr_objects)

            self._compute_statistics(gt_stats, gt_objects, pr_objects, 'd')

            if extended_validation:
                if image.ndim == 2:
                    image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
                utils.draw_objects(image, gt_objects, axis_length=15, thickness=2)
                # Draw over gt objects with longer axes to make sure we see them on gt background.
                utils.draw_objects(image, pr_objects, axis_length=25, thickness=1)
                cv2.imwrite(os.path.join(result_dir, image_file) + '.png', (image * 255).astype(np.uint8))

        self._finalize_statistics(gt_stats, 'Summary based on ground truth:', log_target='sd')

        run_times = np.array(run_times)
        text = f'Images {image_count}'
        if len(run_times):
            text += f', run time {run_times.sum():.3f} s; ' \
                    f'per image min: {run_times.min():.3f}, max: {run_times.max():.3f}, ' \
                    f'mean: {run_times.mean():.3f}, median: {np.median(run_times):.3f}'
        self._log(text)

        print(self._summary_log.getvalue())

        with open(os.path.join(self._model_dir, 'validation.log'), 'w') as f:
            print(self._details_log.getvalue(), file=f)

    def _cross_match_objects(self, gt_objects, pr_objects):
        # Cross-match ground truth and prediction
        gt_unmatched = list(range(len(gt_objects)))
        pr_unmatched = list(range(len(pr_objects)))
        for obj in gt_objects:
            obj.match = None
        for obj in pr_objects:
            obj.match = None
        while True:
            best_match = None
            least_diff = INFINITE_DIFF
            for gti in gt_unmatched:
                gt_object = gt_objects[gti]
                for pri in pr_unmatched:
                    pr_object = pr_objects[pri]
                    diff = self._object_diff(gt_object, pr_object)
                    if abs(diff[0]) < abs(least_diff[0]):
                        least_diff = diff
                        best_match = (gti, pri)
            if best_match is None:
                break
            gt_object = gt_objects[best_match[0]]
            pr_object = pr_objects[best_match[1]]
            gt_object.match = pr_object
            pr_object.match = gt_object
            gt_object.error = least_diff
            pr_object.error = least_diff
            gt_unmatched.remove(best_match[0])
            pr_unmatched.remove(best_match[1])

    def _compute_statistics(self, stats, gt_objects, pr_objects, log_target):
        """
        Compute validation statistics.
        """
        np.set_printoptions(formatter={'float': lambda x: f'{x:0.2f}'})
        for gt_object in gt_objects:
            if gt_object.category not in stats:
                stats[gt_object.category] = CategoryStatistics(gt_object.category)
            cat_stats = stats[gt_object.category]
            cat_stats.total += 1
            if gt_object.match is None:
                cat_stats.missing += 1
                self._log(f'missing, gt {gt_object}', log_target)
            else:
                pr_object = gt_object.match
                error = gt_object.error
                error[1] = np.rad2deg(error[1])
                if pr_object.category == gt_object.category:
                    result_text = 'found'
                    cat_stats.found += 1
                    cat_stats.rmse_position += error[0] ** 2
                    cat_stats.rmse_orientation += error[1] ** 2
                    cat_stats.max_err_position = max(cat_stats.max_err_position, abs(error[0]))
                    cat_stats.max_err_orientation = max(cat_stats.max_err_orientation, abs(error[1]))
                else:
                    result_text = 'misclassified'
                    cat_stats.misclassified[pr_object.category] = cat_stats.misclassified.get(pr_object.category, 0) + 1
                self._log(
                    f'{result_text}, gt: {gt_object} <-> pr {pr_object}, '
                    f'err pos {error[0]:.2f}, err orient {error[1]:.2f}',
                    log_target)
        for pr_object in pr_objects:
            if pr_object.category not in stats:
                stats[pr_object.category] = CategoryStatistics(pr_object.category)
            cat_stats = stats[pr_object.category]
            if pr_object.match is None:
                cat_stats.ufo += 1
                self._log(f'ufo, pr {pr_object}', log_target)
        np.set_printoptions()

    def _finalize_statistics(self, stats, header, log_target):
        """
        Compute total statistics.
        """
        categories = list(stats.keys())
        categories.sort()
        total_stats = CategoryStatistics(CATEGORY_ALL)
        self._log(header, log_target)
        self._log(total_stats.get_header(categories), log_target)
        for category in categories:
            if category == CATEGORY_ALL:
                continue
            cat_stats = stats[category]
            total_stats.add(cat_stats)
            cat_stats.finalize()
            self._log(cat_stats.get_text(categories), log_target)
        total_stats.finalize()
        stats[total_stats.category] = total_stats
        self._log(total_stats.get_text(categories), log_target)

    def _log(self, data, target='ds'):
        """
        Validation log.
        """
        if 'd' in target:
            print(data, file=self._details_log)
        if 's' in target:
            print(data, file=self._summary_log)

    def _object_diff(self, obj1, obj2):
        """
        Compute a difference in position and orientation between 2 objects.
        comparison.
        :return: an array [position_diff, orientation_diff].
        """
        # Assume that objects of different categories are incomparable.
        if obj1.category != obj2.category:
            return INFINITE_DIFF

        position_diff_pix = np.linalg.norm(obj2.origin - obj1.origin)
        orientation_diff = geometry.normalize_angle(obj2.angle - obj1.angle)

        if position_diff_pix > self._cfg['position_diff_thr'] or abs(orientation_diff) > self._cfg['angle_diff_thr']:
            return INFINITE_DIFF

        return np.array([position_diff_pix, orientation_diff])