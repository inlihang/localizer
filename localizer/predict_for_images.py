# Copyright 2018-2020 Ivan Alles. See also the LICENSE file.

import os
import sys

import cv2
import numpy as np

from localizer import predict
from localizer import utils

if len(sys.argv) < 3:
    print(
        "Wrong command line argument. Usage: predict_for_images config_file_name image_dir"
    )
    sys.exit(-1)

config_file_name = sys.argv[1]
image_dir = sys.argv[2]


localizer = predict.Localizer(config_file_name)

total, no_found = 0, 0
for file in os.listdir(image_dir):
    if os.path.splitext(file)[1].lower() not in [".png", ".jpg", ".jpeg"]:
        continue
    image = cv2.imread(os.path.join(image_dir, file))
    image = image.astype(np.float32) / 255

    localizer.diag = True  # Set to True to see diagnostic images
    localizer.diag_dir = os.path.join(
        os.path.dirname(config_file_name), ".temp", "localizer_diag", file
    )

    predictions = localizer.predict(image)

    total += 1
    if not predictions:
        no_found += 1

    result_image = np.copy(image)
    utils.draw_objects(result_image, predictions, axis_length=20, thickness=2)
    result_image = (np.clip(result_image * 255, 0, 255)).astype(np.uint8)

    cv2.imshow("result", result_image)
    cv2.waitKey(500)

print(f"total: {total},\no_found: {no_found}({float(no_found)/total})")
