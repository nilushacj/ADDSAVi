# Copyright 2022 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Video preprocessing ops."""

import abc
import dataclasses
from typing import Optional, Sequence, Tuple, Union
import sys
from absl import logging
from clu import preprocess_spec

import numpy as np
import tensorflow as tf

Features = preprocess_spec.Features
all_ops = lambda: preprocess_spec.get_all_ops(__name__) #gets all the operations in the current script
SEED_KEY = preprocess_spec.SEED_KEY
NOTRACK_BOX = (0., 0., 0., 0.)  # No-track bounding box for padding.
NOTRACK_LABEL = -1

IMAGE = "image"
VIDEO = "video" #key exists
SEGMENTATIONS = "segmentations" #key exists
RAGGED_SEGMENTATIONS = "ragged_segmentations"
SPARSE_SEGMENTATIONS = "sparse_segmentations"
SHAPE = "shape"
PADDING_MASK = "padding_mask"
RAGGED_BOXES = "ragged_boxes"
BOXES = "boxes"
FRAMES = "frames"
FLOW = "flow" #key exists: backward flow and forward flow
DEPTH = "depth" #key exists
ORIGINAL_SIZE = "original_size"
INSTANCE_LABELS = "instance_labels"
INSTANCE_MULTI_LABELS = "instance_multi_labels"

def convert_uint16_to_float(depth, depth_range):
  """
  Converts depth values from uint16 to float32 using a per-frame depth_range.
  
  Args:
    depth: A tf.Tensor of shape (6, H, W, 1) with dtype tf.uint16.
            These values are assumed to be scaled in the [0, 65535] range.
    depth_range: A tf.Tensor of shape (6, 2) with dtype tf.float32.
                  Each row is of the form [min_depth, max_depth] for that frame.
  
  Returns:
    A tf.Tensor of shape (6, H, W, 1) with dtype tf.float32 where each depth value is
    converted to a float value in the range [min_depth, max_depth] corresponding to its frame.
  
  Conversion formula:
    depth_float = min_depth + (depth/65535.0) * (max_depth - min_depth)
  """
  # Cast depth to float32.
  depth_float = tf.cast(depth, tf.float32)
  
  # Extract min and max for each frame (resulting in shape (6,)).
  min_depth = depth_range[:, 0]
  max_depth = depth_range[:, 1]
  
  # Reshape min_depth and max_depth to (6, 1, 1, 1) so they broadcast with depth.
  min_depth = tf.reshape(min_depth, [-1, 1, 1, 1])
  max_depth = tf.reshape(max_depth, [-1, 1, 1, 1])
  
  # Normalize depth values to the [0, 1] range.
  normalized_depth = depth_float / 65535.0  # shape: (6, H, W, 1)
  
  # Scale and shift the normalized depth using the per-frame range.
  converted_depth = min_depth + normalized_depth * (max_depth - min_depth)  # shape: (6, H, W, 1)
  
  depth_inverted = (max_depth - converted_depth)/10.0 # invert the depth

  
  return depth_inverted

def get_resize_small_shape(original_size: Tuple[tf.Tensor, tf.Tensor],
                           small_size: int) -> Tuple[tf.Tensor, tf.Tensor]:
  h, w = original_size
  ratio = (
      tf.cast(small_size, tf.float32) / tf.cast(tf.minimum(h, w), tf.float32))
  h = tf.cast(tf.round(tf.cast(h, tf.float32) * ratio), tf.int32)
  w = tf.cast(tf.round(tf.cast(w, tf.float32) * ratio), tf.int32)
  return h, w

def adjust_small_size(original_size: Tuple[tf.Tensor, tf.Tensor],
                      small_size: int, max_size: int) -> int:
  """Computes the adjusted small size to ensure large side < max_size."""
  h, w = original_size
  min_original_size = tf.cast(tf.minimum(w, h), tf.float32)
  max_original_size = tf.cast(tf.maximum(w, h), tf.float32)
  if max_original_size / min_original_size * small_size > max_size:
    small_size = tf.cast(tf.floor(
        max_size * min_original_size / max_original_size), tf.int32)
  return small_size

def crop_or_pad_boxes(boxes: tf.Tensor, top: int, left: int, height: int,
                      width: int, h_orig: tf.Tensor, w_orig: tf.Tensor):
  """Transforms the relative box coordinates according to the frame crop.

  Note that, if height/width are larger than h_orig/w_orig, this function
  implements the equivalent of padding.

  Args:
    boxes: Tensor of bounding boxes with shape (..., 4).
    top: Top of crop box in absolute pixel coordinates.
    left: Left of crop box in absolute pixel coordinates.
    height: Height of crop box in absolute pixel coordinates.
    width: Width of crop box in absolute pixel coordinates.
    h_orig: Original image height in absolute pixel coordinates.
    w_orig: Original image width in absolute pixel coordinates.
  Returns:
    Boxes tensor with same shape as input boxes but updated values.
  """
  # Video track bound boxes: [num_instances, num_tracks, 4]
  # Image bounding boxes: [num_instances, 4]
  assert boxes.shape[-1] == 4
  seq_len = tf.shape(boxes)[0]
  has_tracks = len(boxes.shape) == 3
  if has_tracks:
    num_tracks = boxes.shape[1]
  else:
    assert len(boxes.shape) == 2
    num_tracks = 1

  # Transform the box coordinates.
  a = tf.cast(tf.stack([h_orig, w_orig]), tf.float32)
  b = tf.cast(tf.stack([top, left]), tf.float32)
  c = tf.cast(tf.stack([height, width]), tf.float32)
  boxes = tf.reshape(
      (tf.reshape(boxes, (seq_len, num_tracks, 2, 2)) * a - b) / c,
      (seq_len, num_tracks, len(NOTRACK_BOX)))

  # Filter the valid boxes.
  boxes = tf.minimum(tf.maximum(boxes, 0.0), 1.0)
  if has_tracks:
    cond = tf.reduce_all((boxes[:, :, 2:] - boxes[:, :, :2]) > 0.0, axis=-1)
    boxes = tf.where(cond[:, :, tf.newaxis], boxes, NOTRACK_BOX)
  else:
    boxes = tf.reshape(boxes, (seq_len, 4))

  return boxes

def flow_tensor_to_rgb_tensor(motion_image, flow_scaling_factor=50.):
  """Visualizes flow motion image as an RGB image.

  Similar as the flow_to_rgb function, but with tensors.

  Args:
    motion_image: A tensor either of shape [batch_sz, height, width, 2] or of
      shape [height, width, 2]. motion_image[..., 0] is flow in x and
      motion_image[..., 1] is flow in y.
    flow_scaling_factor: How much to scale flow for visualization.

  Returns:
    A visualization tensor with same shape as motion_image, except with three
    channels. The dtype of the output is tf.uint8.
  """

  hypot = lambda a, b: (a ** 2.0 + b ** 2.0) ** 0.5  # sqrt(a^2 + b^2)

  height, width = motion_image.get_shape().as_list()[-3:-1]  # pytype: disable=attribute-error  # allow-recursive-types
  scaling = flow_scaling_factor / hypot(height, width)
  x, y = motion_image[..., 0], motion_image[..., 1]
  motion_angle = tf.atan2(y, x)
  motion_angle = (motion_angle / np.math.pi + 1.0) / 2.0
  motion_magnitude = hypot(y, x)
  motion_magnitude = tf.clip_by_value(motion_magnitude * scaling, 0.0, 1.0)
  value_channel = tf.ones_like(motion_angle)
  flow_hsv = tf.stack([motion_angle, motion_magnitude, value_channel], axis=-1)
  flow_rgb = tf.image.convert_image_dtype(
      tf.image.hsv_to_rgb(flow_hsv), tf.uint8)
  return flow_rgb

def get_paddings(image_shape: tf.TensorShape,
                 size: Union[int, Tuple[int, int], Sequence[int]],
                 pre_spatial_dim: Optional[int] = None,
                 allow_crop: bool = True) -> tf.Tensor:
  """Returns paddings tensors for tf.pad operation.

  Args:
    image_shape: The shape of the Tensor to be padded. The shape can be
      [..., N, H, W, C] or [..., H, W, C]. The paddings are computed for H, W
      and optionally N dimensions.
    size: The total size for the H and W dimensions to pad to.
    pre_spatial_dim: Optional, additional padding dimension before the spatial
      dimensions. It is only used if given and if len(shape) > 3.
    allow_crop: If size is bigger than requested max size, padding will be
      negative. If allow_crop is true, negative padding values will be set to 0.

  Returns:
    Paddings the given tensor shape.
  """
  assert image_shape.shape.rank == 1
  if isinstance(size, int):
    size = (size, size)
  h, w = image_shape[-3], image_shape[-2]
  # Spatial padding.
  paddings = [
      tf.stack([0, size[0] - h]),
      tf.stack([0, size[1] - w]),
      tf.stack([0, 0])
  ]
  ndims = len(image_shape)  # pytype: disable=wrong-arg-types
  # Prepend padding for temporal dimension or number of instances.
  if pre_spatial_dim is not None and ndims > 3:
    paddings = [[0, pre_spatial_dim - image_shape[-4]]] + paddings
  # Prepend with non-padded dimensions if available.
  if ndims > len(paddings):
    paddings = [[0, 0]] * (ndims - len(paddings)) + paddings
  if allow_crop:
    paddings = tf.maximum(paddings, 0)
  return tf.stack(paddings)

def convert_to_correct_format(bboxes_tensor: tf.Tensor, max_instances: int, bbox_threshold: float, valid_classes: tf.Tensor) -> tf.Tensor:
  """
  Converts bounding box tensor from (6, 150, 5) format to (6, max_instances, 4) format,
  while filtering rows based on class values, small bounding boxes, and then sorting the
  valid detections in descending order by area. Any remaining rows are zero-padded.

  Args:
      bboxes_tensor: A 3D tensor of shape (6, 150, 5), where the 5 values represent normalized bounding box coordinates:
                    [ymin, ymax, xmin, xmax, class].
      max_instances: The maximum number of bounding boxes per frame.
      bbox_thresold: Threshold to filter out small bounding boxes based on area
      valid_classes: A 1D tensor of allowed class values (must be a tf.Tensor before calling the function).

  Returns:
      A tensor of shape (6, max_instances, 4), where the 4 values are [ymin, xmin, ymax, xmax].
  """

  # Ensure the input tensor has the expected shape
  tf.debugging.assert_shapes([(bboxes_tensor, (6, 150, 5))])

  # Extract relevant columns: 
  ymin = bboxes_tensor[..., 0]  # Shape (6, 150)
  ymax = bboxes_tensor[..., 1]  # Shape (6, 150)
  xmin = bboxes_tensor[..., 2]  # Shape (6, 150)
  xmax = bboxes_tensor[..., 3]  # Shape (6, 150)
  class_values = tf.cast(bboxes_tensor[..., -1], tf.int32)  # Shape (6, 150)

  # Step 1: Create a valid mask based on class
  expanded_classes = tf.expand_dims(class_values, -1)  # Shape (6, 150, 1)
  matches = tf.equal(expanded_classes, valid_classes)  # Shape (6, 150, num_valid_classes)
  valid_mask = tf.reduce_any(matches, axis=-1)  # Shape (6, 150), True for valid rows

  # Step 2: Compute bounding box area and apply bbox_thresold
  width = (xmax - xmin) 
  height= (ymax - ymin)
  area = width * height # Shape (6, 150)
  valid_area_mask = area >= bbox_threshold #True for bbs with area more than the thresh
  
  # Step 3 Combine the masks 
  combined_mask = tf.logical_and(valid_mask, valid_area_mask) # Shape (6,150)

  # Step 4: Zero-pad invalid rows
  zero_tensor = tf.zeros_like(ymin)  # Create a zero tensor of the same shape
  xmin = tf.where(combined_mask, xmin, zero_tensor)  
  ymin = tf.where(combined_mask, ymin, zero_tensor)
  xmax = tf.where(combined_mask, xmax, zero_tensor)
  ymax = tf.where(combined_mask, ymax, zero_tensor)

  # Also zero out the area for invalid boxes so they sort to the end.
  area = tf.where(combined_mask, area, tf.zeros_like(area))

  # Step 5: Stack and limit to max_instances
  corners = tf.stack([ymin, xmin, ymax, xmax], axis=-1)  # Shape (6, 150, 4)

  # Step 6: For each frame, sort the boxes in descending order by area.
  # Get the sorted indices along axis=1 (the detection axis) for each frame.
  sorted_indices = tf.argsort(area, direction='DESCENDING', stable=True)  # shape: (6, 150)
  # Gather the boxes according to these sorted indices. Use batch_dims=1.
  sorted_corners = tf.gather(corners, sorted_indices, axis=1, batch_dims=1)  # shape: (6, 150, 4)

  # Step 7: Limit to max_instances.
  # If there are fewer valid boxes, the zero-padded ones (with zero area) will remain at the end.
  sorted_corners = sorted_corners[:, :max_instances, :]  # shape: (6, max_instances, 4)
  
  return sorted_corners


@dataclasses.dataclass
class FromWaymoOpen:
  camera_key: str
  max_num_bboxes: int
  bbox_threshold: float
  video_key: str = VIDEO
  depth_key: str = DEPTH
  shape_key: str = SHAPE
  boxes_key: str = BOXES
  padding_mask_key: str = PADDING_MASK

  def __call__(self, features: Features) -> Features:

    features_new = {}
    # ---- Get seed ----
    if "rng" in features:
      features_new[SEED_KEY] = features.pop("rng")

    # ---- Extract camera video (length = 6) ----
    orig_video = features[self.camera_key]["image"] 
    assert orig_video.dtype == tf.uint8 
    video = tf.image.convert_image_dtype(orig_video, tf.float32) # convert to float and normalize 
    video = tf.image.resize(video, [128, 192], method="bilinear") #TODO: check resize method (aspect ratio issue) - after 1st round of training
    features_new[self.video_key] = video 

    # ---- Store video shape (e.g. for correct evaluation metrics). ----
    features_new[self.shape_key] = tf.shape(video)
    # ---- Store padding mask ----
    features_new[self.padding_mask_key] = tf.cast(
      tf.ones_like(video)[..., 0], tf.uint8) 

    # ---- Extract depth ----
    depth_range     = features[self.camera_key]["depth_range"]
    cam_depth_uint  = features[self.camera_key]["depth"] 
    cam_depth_float = convert_uint16_to_float(cam_depth_uint, depth_range)
    cam_depth_float = tf.image.resize(cam_depth_float, [128, 192], method="nearest") 
    features_new[self.depth_key] = cam_depth_float

    # ---- Get bounding boxes ----
    cam_bboxes = features[self.camera_key]["detections"]

    allowed_classes  = tf.constant([0, 1, 2], dtype=tf.int32) # vehicles, peds, cyclists
    converted_bboxes = convert_to_correct_format(cam_bboxes, self.max_num_bboxes, self.bbox_threshold, allowed_classes) # TODO: integrity check

    features_new[self.boxes_key] = converted_bboxes #format to [ymin, xmin, ymax, xmax]

    return features_new


@dataclasses.dataclass
class AddTemporalAxis:
  """Lift images to videos by adding a temporal axis at the beginning.

  We need to distinguish two cases because `image_ops.py` uses
  ORIGINAL_SIZE = [H,W] and `video_ops.py` uses SHAPE = [T,H,W,C]:
  a) The features are fed from image ops: ORIGINAL_SIZE is converted
    to SHAPE ([H,W] -> [1,H,W,C]) and removed from the features.
    Typical use case: Evaluation of GV image tasks in a video setting. This op
    is added after the image preprocessing in order not to change the standard
    image preprocessing.
  b) The features are fed from video ops: The image SHAPE is lifted to a video
    SHAPE ([H,W,C] -> [1,H,W,C]).
    Typical use case: Training using images in a video setting. This op is added
    before the video preprocessing in order not to change the standard video
    preprocessing.
  """

  image_key: str = IMAGE
  video_key: str = VIDEO
  boxes_key: str = BOXES
  padding_mask_key: str = PADDING_MASK
  segmentations_key: str = SEGMENTATIONS
  sparse_segmentations_key: str = SPARSE_SEGMENTATIONS
  shape_key: str = SHAPE
  original_size_key: str = ORIGINAL_SIZE

  def __call__(self, features: Features) -> Features:
    assert self.image_key in features

    features_new = {}
    for k, v in features.items():
      if k == self.image_key:
        features_new[self.video_key] = v[tf.newaxis]
      elif k in (self.padding_mask_key, self.boxes_key, self.segmentations_key,
                 self.sparse_segmentations_key):
        features_new[k] = v[tf.newaxis]
      elif k == self.original_size_key:
        pass  # See comment in the docstring of the class.
      else:
        features_new[k] = v

    if self.original_size_key in features:
      # The features come from an image preprocessing pipeline.
      shape = tf.concat([[1], features[self.original_size_key],
                         [features[self.image_key].shape[-1]]],  # pytype: disable=attribute-error  # allow-recursive-types
                        axis=0)
    elif self.shape_key in features:
      # The features come from a video preprocessing pipeline.
      shape = tf.concat([[1], features[self.shape_key]], axis=0)
    else:
      shape = tf.shape(features_new[self.video_key])
    features_new[self.shape_key] = shape

    if self.padding_mask_key not in features_new:
      features_new[self.padding_mask_key] = tf.cast(
          tf.ones_like(features_new[self.video_key])[..., 0], tf.uint8)

    return features_new


class VideoPreprocessOp(abc.ABC):
  """Base class for all video preprocess ops."""

  video_key: str = VIDEO
  segmentations_key: str = SEGMENTATIONS
  padding_mask_key: str = PADDING_MASK
  boxes_key: str = BOXES
  flow_key: str = FLOW
  depth_key: str = DEPTH
  sparse_segmentations_key: str = SPARSE_SEGMENTATIONS

  def __call__(self, features: Features) -> Features:
    # Get current video shape.
    video_shape = tf.shape(features[self.video_key])
    # Assemble all feature keys that the op should be applied on.
    all_keys = [
        self.video_key, self.segmentations_key, self.padding_mask_key,
        self.flow_key, self.depth_key, self.sparse_segmentations_key,
        self.boxes_key
    ]
    # Apply the op to all features.
    for key in all_keys:
      if key in features:
        features[key] = self.apply(features[key], key, video_shape)
    return features

  @abc.abstractmethod
  def apply(self, tensor: tf.Tensor, key: str,
            video_shape: tf.TensorShape) -> tf.Tensor:
    """Returns the transformed tensor.

    Args:
      tensor: Any of a set of different video modalites, e.g video, flow,
        bounding boxes, etc.
      key: a string that indicates what feature the tensor represents so that
        the apply function can take that into account.
      video_shape: The shape of the video (which is necessary for some
        transformations).
    """
    pass


class RandomVideoPreprocessOp(VideoPreprocessOp):
  """Base class for all random video preprocess ops."""

  def __call__(self, features: Features) -> Features:
    if features.get(SEED_KEY) is None:
      logging.warning(
          "Using random operation without seed. To avoid this "
          "please provide a seed in feature %s.", SEED_KEY)
      op_seed = tf.random.uniform(shape=(2,), maxval=2**32, dtype=tf.int64)
    else:
      features[SEED_KEY], op_seed = tf.unstack(
          tf.random.experimental.stateless_split(features[SEED_KEY]))
    # Get current video shape.
    video_shape = tf.shape(features[self.video_key])
    # Assemble all feature keys that the op should be applied on.
    all_keys = [
        self.video_key, self.segmentations_key, self.padding_mask_key,
        self.flow_key, self.depth_key, self.sparse_segmentations_key,
        self.boxes_key
    ]
    # Apply the op to all features.
    for key in all_keys:
      if key in features:
        features[key] = self.apply(features[key], op_seed, key, video_shape)

    return features

  @abc.abstractmethod
  def apply(self, tensor: tf.Tensor, seed: tf.Tensor, key: str,
            video_shape: tf.TensorShape) -> tf.Tensor:
    """Returns the transformed tensor.

    Args:
      tensor: Any of a set of different video modalites, e.g video, flow,
        bounding boxes, etc.
      seed: A random seed.
      key: a string that indicates what feature the tensor represents so that
        the apply function can take that into account.
      video_shape: The shape of the video (which is necessary for some
        transformations).
    """
    pass


@dataclasses.dataclass
class ResizeSmall(VideoPreprocessOp):
  """Resizes the smaller (spatial) side to `size` keeping aspect ratio.

  Attr:
    size: An integer representing the new size of the smaller side of the input.
    max_size: If set, an integer representing the maximum size in terms of the
      largest side of the input.
  """

  size: int
  max_size: Optional[int] = None

  def apply(self, tensor, key=None, video_shape=None):
    """See base class."""

    # Boxes are defined in normalized image coordinates and are not affected.
    if key == self.boxes_key:
      return tensor

    if key in (self.padding_mask_key, self.segmentations_key):
      tensor = tensor[..., tf.newaxis]
    elif key == self.sparse_segmentations_key:
      tensor = tf.reshape(tensor,
                          (-1, tf.shape(tensor)[2], tf.shape(tensor)[3], 1))

    h, w = tf.shape(tensor)[1], tf.shape(tensor)[2]

    # Determine resize method based on dtype (e.g. segmentations are int).
    if tensor.dtype.is_integer:
      resize_method = "nearest"
    else:
      resize_method = "bilinear"

    # Clip size to max_size if needed.
    small_size = self.size
    if self.max_size is not None:
      small_size = adjust_small_size(
          original_size=(h, w), small_size=small_size, max_size=self.max_size)
    new_h, new_w = get_resize_small_shape(
        original_size=(h, w), small_size=small_size)
    tensor = tf.image.resize(tensor, [new_h, new_w], method=resize_method)

    # Flow needs to be rescaled according to the new size to stay valid.
    if key == self.flow_key:
      scale_h = tf.cast(new_h, tf.float32) / tf.cast(h, tf.float32)
      scale_w = tf.cast(new_w, tf.float32) / tf.cast(w, tf.float32)
      scale = tf.reshape(tf.stack([scale_h, scale_w], axis=0), (1, 2))
      # Optionally repeat scale in case both forward and backward flow are
      # stacked in the last dimension.
      scale = tf.repeat(scale, tf.shape(tensor)[-1] // 2, axis=0)
      scale = tf.reshape(scale, (1, 1, 1, tf.shape(tensor)[-1]))
      tensor *= scale

    if key in (self.padding_mask_key, self.segmentations_key):
      tensor = tensor[..., 0]
    elif key == self.sparse_segmentations_key:
      tensor = tf.reshape(tensor, (video_shape[0], -1, new_h, new_w))

    return tensor


@dataclasses.dataclass
class CentralCrop(VideoPreprocessOp):
  """Makes central (spatial) crop of a given size.

  Attr:
    height: An integer representing the height of the crop.
    width: An (optional) integer representing the width of the crop. Make square
      crop if width is not provided.
  """

  height: int
  width: Optional[int] = None

  def apply(self, tensor, key=None, video_shape=None):
    """See base class."""
    if key == self.boxes_key:
      width = self.width or self.height
      h_orig, w_orig = video_shape[1], video_shape[2]
      top = (h_orig - self.height) // 2
      left = (w_orig - width) // 2
      tensor = crop_or_pad_boxes(tensor, top, left, self.height,
                                 width, h_orig, w_orig)
      return tensor
    else:
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., tf.newaxis]
      seq_len, n_channels = tensor.get_shape()[0], tensor.get_shape()[3]
      h_orig, w_orig = tf.shape(tensor)[1], tf.shape(tensor)[2]
      width = self.width or self.height
      crop_size = (seq_len, self.height, width, n_channels)
      top = (h_orig - self.height) // 2
      left = (w_orig - width) // 2
      tensor = tf.image.crop_to_bounding_box(tensor, top, left, self.height,
                                             width)
      tensor = tf.ensure_shape(tensor, crop_size)
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., 0]
      return tensor


@dataclasses.dataclass
class CropOrPad(VideoPreprocessOp):
  """Spatially crops or pads a video to a specified size.

  Attr:
    height: An integer representing the new height of the video.
    width: An integer representing the new width of the video.
    allow_crop: A boolean indicating if cropping is allowed.
  """

  height: int
  width: int
  allow_crop: bool = True

  def apply(self, tensor, key=None, video_shape=None):
    """See base class."""
    if key == self.boxes_key:
      # Pad and crop the spatial dimensions.
      h_orig, w_orig = video_shape[1], video_shape[2]
      if self.allow_crop:
        # After cropping, the frame shape is always [self.height, self.width].
        height, width = self.height, self.width
      else:
        # If only padding is performed, the frame size is at least
        # [self.height, self.width].
        height = tf.maximum(h_orig, self.height)
        width = tf.maximum(w_orig, self.width)
      tensor = crop_or_pad_boxes(
          tensor,
          top=0,
          left=0,
          height=height,
          width=width,
          h_orig=h_orig,
          w_orig=w_orig)
      return tensor
    elif key == self.sparse_segmentations_key:
      seq_len = tensor.get_shape()[0]
      paddings = get_paddings(
          tf.shape(tensor[..., tf.newaxis]), (self.height, self.width),
          allow_crop=self.allow_crop)[:-1]
      tensor = tf.pad(tensor, paddings, constant_values=0)
      if self.allow_crop:
        tensor = tensor[..., :self.height, :self.width]
      tensor = tf.ensure_shape(
          tensor, (seq_len, None, self.height, self.width))
      return tensor
    else:
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., tf.newaxis]
      seq_len, n_channels = tensor.get_shape()[0], tensor.get_shape()[3]
      paddings = get_paddings(
          tf.shape(tensor), (self.height, self.width),
          allow_crop=self.allow_crop)
      tensor = tf.pad(tensor, paddings, constant_values=0)
      if self.allow_crop:
        tensor = tensor[:, :self.height, :self.width, :]
      tensor = tf.ensure_shape(tensor,
                               (seq_len, self.height, self.width, n_channels))
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., 0]
      return tensor


@dataclasses.dataclass
class RandomCrop(RandomVideoPreprocessOp):
  """Gets a random (width, height) crop of input video.

  Assumption: Height and width are the same for all video-like modalities.

  Attr:
    height: An integer representing the height of the crop.
    width: An integer representing the width of the crop.
  """

  height: int
  width: int

  def apply(self, tensor, seed, key=None, video_shape=None):
    """See base class."""
    if key == self.boxes_key:
      # We copy the random generation part from tf.image.stateless_random_crop
      # to generate exactly the same offset as for the video.
      crop_size = (video_shape[0], self.height, self.width, video_shape[-1])
      size = tf.convert_to_tensor(crop_size, tf.int32)
      limit = video_shape - size + 1
      offset = tf.random.stateless_uniform(
          tf.shape(video_shape), dtype=tf.int32, maxval=tf.int32.max,
          seed=seed) % limit
      tensor = crop_or_pad_boxes(tensor, offset[1], offset[2], self.height,
                                 self.width, video_shape[1], video_shape[2])
      return tensor
    elif key == self.sparse_segmentations_key:
      raise NotImplementedError("Sparse segmentations aren't supported yet")
    else:
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., tf.newaxis]
      seq_len, n_channels = tensor.get_shape()[0], tensor.get_shape()[3]
      crop_size = (seq_len, self.height, self.width, n_channels)
      tensor = tf.image.stateless_random_crop(tensor, size=crop_size, seed=seed)
      tensor = tf.ensure_shape(tensor, crop_size)
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., 0]
      return tensor


@dataclasses.dataclass
class DropFrames(VideoPreprocessOp):
  """Subsamples a video by skipping frames.

  Attr:
    frame_skip: An integer representing the subsampling frequency of the video,
      where 1 means no frames are skipped, 2 means every other frame is skipped,
      and so forth.
  """

  frame_skip: int

  def apply(self, tensor, key=None, video_shape=None):
    """See base class."""
    del key
    del video_shape
    tensor = tensor[::self.frame_skip]
    new_length = tensor.get_shape()[0]
    tensor = tf.ensure_shape(tensor, [new_length] + tensor.get_shape()[1:])
    return tensor


@dataclasses.dataclass
class TemporalCropOrPad(VideoPreprocessOp):
  """Crops or pads a video in time to a specified length.

  Attr:
    length: An integer representing the new length of the video.
    allow_crop: A boolean, specifying whether temporal cropping is allowed. If
      False, will throw an error if length of the video is more than "length"
  """

  length: int
  allow_crop: bool = True

  def _apply(self, tensor, constant_values):
    frames_to_pad = self.length - tf.shape(tensor)[0]
    if self.allow_crop:
      frames_to_pad = tf.maximum(frames_to_pad, 0)
    tensor = tf.pad(
        tensor, ((0, frames_to_pad),) + ((0, 0),) * (len(tensor.shape) - 1),
        constant_values=constant_values)
    tensor = tensor[:self.length]
    tensor = tf.ensure_shape(tensor, [self.length] + tensor.get_shape()[1:])
    return tensor

  def apply(self, tensor, key=None, video_shape=None):
    """See base class."""
    del video_shape
    if key == self.boxes_key:
      constant_values = NOTRACK_BOX[0]
    else:
      constant_values = 0
    return self._apply(tensor, constant_values=constant_values)


@dataclasses.dataclass
class TemporalRandomWindow(RandomVideoPreprocessOp):
  """Gets a random slice (window) along 0-th axis of input tensor.

  Pads the video if the video length is shorter than the provided length.

  Assumption: The number of frames is the same for all video-like modalities.

  Attr:
    length: An integer representing the new length of the video.
  """

  length: int

  def _apply(self, tensor, seed, constant_values):
    length = tf.minimum(self.length, tf.shape(tensor)[0])
    frames_to_pad = tf.maximum(self.length - tf.shape(tensor)[0], 0)
    window_size = tf.concat(([length], tf.shape(tensor)[1:]), axis=0)
    tensor = tf.image.stateless_random_crop(tensor, size=window_size, seed=seed)
    tensor = tf.pad(
        tensor, ((0, frames_to_pad),) + ((0, 0),) * (len(tensor.shape) - 1),
        constant_values=constant_values)
    tensor = tf.ensure_shape(tensor, [self.length] + tensor.get_shape()[1:])
    return tensor

  def apply(self, tensor, seed, key=None, video_shape=None):
    """See base class."""
    del video_shape
    if key == self.boxes_key:
      constant_values = NOTRACK_BOX[0]
    else:
      constant_values = 0
    return self._apply(tensor, seed, constant_values=constant_values)


@dataclasses.dataclass
class TemporalRandomStridedWindow(RandomVideoPreprocessOp):
  """Gets a random strided slice (window) along 0-th axis of input tensor.

  This op is like TemporalRandomWindow but it samples from one of a set of
  strides of the video, whereas TemporalRandomWindow will densely sample from
  all possible slices of `length` frames from the video.

  For the following video and `length=3`: [1, 2, 3, 4, 5, 6, 7, 8, 9]

  This op will return one of [1, 2, 3], [4, 5, 6], or [7, 8, 9]

  This pads the video if the video length is shorter than the provided length.

  Assumption: The number of frames is the same for all video-like modalities.

  Attr:
    length: An integer representing the new length of the video and the sampling
      stride width.
  """

  length: int

  def _apply(self, tensor: tf.Tensor, seed: Sequence[int],
             constant_values: Union[int, float]) -> tf.Tensor:
    """Applies the strided crop operation to the video tensor."""
    num_frames = tf.shape(tensor)[0]
    num_crop_points = tf.cast(tf.math.ceil(num_frames / self.length), tf.int32)
    crop_point = tf.random.stateless_uniform(
        shape=(), minval=0, maxval=num_crop_points, dtype=tf.int32, seed=seed)
    crop_point *= self.length
    frames_sample = tensor[crop_point:crop_point + self.length]
    frames_to_pad = tf.maximum(self.length - tf.shape(frames_sample)[0], 0)
    frames_sample = tf.pad(
        frames_sample,
        ((0, frames_to_pad),) + ((0, 0),) * (len(frames_sample.shape) - 1),
        constant_values=constant_values)
    frames_sample = tf.ensure_shape(frames_sample, [self.length] +
                                    frames_sample.get_shape()[1:])
    return frames_sample

  def apply(self, tensor, seed, key=None, video_shape=None):
    """See base class."""
    del video_shape
    if key == self.boxes_key:
      constant_values = NOTRACK_BOX[0]
    else:
      constant_values = 0
    return self._apply(tensor, seed, constant_values=constant_values)


@dataclasses.dataclass
class TransformDepth:
  """Applies one of several possible transformations to depth features."""
  transform: str
  depth_key: str = DEPTH

  def __call__(self, features: Features) -> Features:
    
    if self.depth_key in features:
      if self.transform == "log":
        depth_norm = tf.math.log(features[self.depth_key])
      elif self.transform == "log_plus":
        depth_norm = tf.math.log(1. + features[self.depth_key])
      elif self.transform == "invert_plus":
        depth_norm = 1. / (1. + features[self.depth_key])
      elif self.transform == "exp_rel":
        depth_norm = tf.math.exp((1.6 * features[self.depth_key]) / 850) - 1
      else:
        raise ValueError(f"Unknown depth transformation {self.transform}")

      features[self.depth_key] = depth_norm

    return features


@dataclasses.dataclass
class RandomResizedCrop(RandomVideoPreprocessOp):
  """Random-resized crop for each of the two views.

  Assumption: Height and width are the same for all video-like modalities.

  We randomly crop the input and record the transformation this crop corresponds
  to as a new feature. Croped images are resized to (height, width). Boxes are
  corrected adjusted and boxes outside the crop are discarded. Flow is rescaled
  so as to be pixel accurate after the operation. lidar_points_2d are
  transformed using the computed transformation. These points may lie outside
  the image after the operation.

  Attr:
    height: An integer representing the height to resize to.
    width: An integer representing the width to resize to.
    min_object_covered, aspect_ratio_range, area_range, max_attempts: See
      docstring of `stateless_sample_distorted_bounding_box`. Aspect ratio range
      has not been scaled by target aspect ratio. This differs from other
      implementations of this data augmentation.
    relative_box_area_threshold: If ratio of areas before and after cropping are
      lower than this threshold, then the box is discarded (set to NOTRACK_BOX).
  """
  # Target size.
  height: int
  width: int

  # Crop sampling attributes.
  min_object_covered: float = 0.1
  aspect_ratio_range: Tuple[float, float] = (3. / 4., 4. / 3.)
  area_range: Tuple[float, float] = (0.08, 1.0)
  max_attempts: int = 100

  # Box retention attributes
  relative_box_area_threshold: float = 0.0

  def apply(self, tensor: tf.Tensor, seed: tf.Tensor, key: str,
            video_shape: tf.Tensor) -> tf.Tensor:
    """Applies the crop operation on tensor."""
    param = self.sample_augmentation_params(video_shape, seed)
    si, sj = param[0], param[1]
    crop_h, crop_w = param[2], param[3]

    to_float32 = lambda x: tf.cast(x, tf.float32)

    if key == self.boxes_key:
      # First crop the boxes.
      cropped_boxes = crop_or_pad_boxes(
          tensor, si, sj,
          crop_h, crop_w,
          video_shape[1], video_shape[2])
      # We do not need to scale the boxes because they are in normalized coords.
      resized_boxes = cropped_boxes
      # Lastly detects NOTRACK_BOX boxes and avoid manipulating those.
      no_track_boxes = tf.convert_to_tensor(NOTRACK_BOX)
      no_track_boxes = tf.reshape(no_track_boxes, [1, 4])
      resized_boxes = tf.where(
          tf.reduce_all(tensor == no_track_boxes, axis=-1, keepdims=True),
          tensor, resized_boxes)

      if self.relative_box_area_threshold > 0:
        # Thresholds boxes that have been cropped too much, as in their area is
        # lower, in relative terms, than `relative_box_area_threshold`.
        area_before_crop = tf.reduce_prod(tensor[..., 2:] - tensor[..., :2],
                                          axis=-1)
        # Sets minimum area_before_crop to 1e-8 we avoid divisions by 0.
        area_before_crop = tf.maximum(area_before_crop,
                                      tf.zeros_like(area_before_crop) + 1e-8)
        area_after_crop = tf.reduce_prod(
            resized_boxes[..., 2:] - resized_boxes[..., :2], axis=-1)
        # As the boxes have normalized coordinates, they need to be rescaled to
        # be compared against the original uncropped boxes.
        scale_x = to_float32(crop_w) / to_float32(self.width)
        scale_y = to_float32(crop_h) / to_float32(self.height)
        area_after_crop *= scale_x * scale_y

        ratio = area_after_crop / area_before_crop
        return tf.where(
            tf.expand_dims(ratio > self.relative_box_area_threshold, -1),
            resized_boxes, no_track_boxes)

      else:
        return resized_boxes

    else:
      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., tf.newaxis]

      # Crop.
      seq_len, n_channels = tensor.get_shape()[0], tensor.get_shape()[3]
      crop_size = (seq_len, crop_h, crop_w, n_channels)
      tensor = tf.slice(tensor, tf.stack([0, si, sj, 0]), crop_size)

      # Resize.
      resize_method = tf.image.ResizeMethod.BILINEAR
      if (tensor.dtype == tf.int32 or tensor.dtype == tf.int64 or
          tensor.dtype == tf.uint8):
        resize_method = tf.image.ResizeMethod.NEAREST_NEIGHBOR
      tensor = tf.image.resize(tensor, [self.height, self.width],
                               method=resize_method)
      out_size = (seq_len, self.height, self.width, n_channels)
      tensor = tf.ensure_shape(tensor, out_size)

      if key == self.flow_key:
        # Rescales optical flow.
        scale_x = to_float32(self.width) / to_float32(crop_w)
        scale_y = to_float32(self.height) / to_float32(crop_h)
        tensor = tf.stack(
            [tensor[..., 0] * scale_y, tensor[..., 1] * scale_x], axis=-1)

      if key in (self.padding_mask_key, self.segmentations_key):
        tensor = tensor[..., 0]
      return tensor

  def sample_augmentation_params(self, video_shape: tf.Tensor, rng: tf.Tensor):
    """Sample a random bounding box for the crop."""
    sample_bbox = tf.image.stateless_sample_distorted_bounding_box(
        video_shape[1:],
        bounding_boxes=tf.constant([0.0, 0.0, 1.0, 1.0],
                                   dtype=tf.float32, shape=[1, 1, 4]),
        seed=rng,
        min_object_covered=self.min_object_covered,
        aspect_ratio_range=self.aspect_ratio_range,
        area_range=self.area_range,
        max_attempts=self.max_attempts,
        use_image_if_no_bounding_boxes=True)
    bbox_begin, bbox_size, _ = sample_bbox

    # The specified bounding box provides crop coordinates.
    offset_y, offset_x, _ = tf.unstack(bbox_begin)
    target_height, target_width, _ = tf.unstack(bbox_size)

    return tf.stack([offset_y, offset_x, target_height, target_width])

  def estimate_transformation(self, param: tf.Tensor, video_shape: tf.Tensor
                              ) -> tf.Tensor:
    """Computes the affine transformation for crop params.

    Args:
      param: Crop parameters in the [y, x, h, w] format of shape [4,].
      video_shape: Unused.

    Returns:
      Affine transformation of shape [3, 3] corresponding to cropping the image
      at [y, x] of size [h, w] and resizing it into [self.height, self.width].
    """
    del video_shape
    crop = tf.cast(param, tf.float32)
    si, sj = crop[0], crop[1]
    crop_h, crop_w = crop[2], crop[3]
    ei, ej = si + crop_h - 1.0, sj + crop_w - 1.0
    h, w = float(self.height), float(self.width)

    a1 = (ei - si + 1.)/h
    a2 = 0.
    a3 = si - 0.5 + a1 / 2.
    a4 = 0.
    a5 = (ej - sj + 1.)/w
    a6 = sj - 0.5 + a5 / 2.
    affine = tf.stack([a1, a2, a3, a4, a5, a6, 0., 0., 1.])
    return tf.reshape(affine, [3, 3])