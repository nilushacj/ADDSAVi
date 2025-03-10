from typing import Tuple, List, Dict
import tensorflow as tf
import tensorflow_datasets as tfds
import functools
from clu import deterministic_data

import ml_collections
import jax
import jax.numpy as jnp

Array = jnp.ndarray
PRNGKey = Array
from savi.lib import preprocessing
from clu import preprocess_spec

def preprocess_example(features:Dict[str, tf.Tensor],
                       preprocess_strs:List[str])->Dict[str, tf.Tensor]:
  """Processes a single data example.

  Args:
    features: A dictionary containing the tensors of a single data example.
    preprocess_strs: List of strings, describing one preprocessing operation
      each, in clu.preprocess_spec format.

  Returns:
    Dictionary containing the preprocessed tensors of a single data example.
  """
  all_ops = preprocessing.all_ops()
  
  # Parses all ops from above (see SS for printed strings)
  preprocess_fn = preprocess_spec.parse("|".join(preprocess_strs), all_ops)

  return preprocess_fn(features)  # pytype: disable=bad-return-type  # allow-recursive-types


def get_batch_dims(global_batch_size: int) -> List[int]:
  """Gets the first two axis sizes for data batches.

  Args:
    global_batch_size: Integer, the global batch size (across all devices).

  Returns:
    List of batch dimensions

  Raises:
    ValueError if the requested dimensions don't make sense with the
      number of devices.
  """
  num_local_devices = jax.local_device_count()
  if global_batch_size % jax.host_count() != 0:
    raise ValueError(f"Global batch size {global_batch_size} not evenly "
                     f"divisble with {jax.host_count()}.")
  per_host_batch_size = global_batch_size // jax.host_count()
  if per_host_batch_size % num_local_devices != 0:
    raise ValueError(f"Global batch size {global_batch_size} not evenly "
                     f"divisible with {jax.host_count()} hosts with a per host "
                     f"batch size of {per_host_batch_size} and "
                     f"{num_local_devices} local devices. ")
  return [num_local_devices, per_host_batch_size // num_local_devices]
  

def create_datasets(config: ml_collections.ConfigDict,
                    data_rng: PRNGKey) -> Tuple[tf.data.Dataset, tf.data.Dataset]:
  
  dataset_builder = tfds.builder(config.data.name, data_dir=config.data.data_dir)
  batch_dims = get_batch_dims(config.batch_size)

  #---- Set training ----

  train_preprocess_fn = functools.partial(
    preprocess_example, preprocess_strs=config.preproc_train
  )

  train_split = tfds.split_for_jax_process('train', drop_remainder=True)

  train_ds = deterministic_data.create_dataset(
    dataset_builder=dataset_builder, #as_dataset() method is needed
    split=train_split, 
    batch_dims=batch_dims, 
    rng=data_rng, 
    preprocess_fn=train_preprocess_fn, 
    cache=False, 
    num_epochs=None, 
    shuffle=True, 
    shuffle_buffer_size=config.data.shuffle_buffer_size
  )

  # ---- Set validation ----

  eval_preprocess_fn = functools.partial(
    preprocess_example, preprocess_strs=config.preproc_eval
  )

  eval_split = tfds.split_for_jax_process('validation', drop_remainder=True)

  # -- Function to create standard input pipeline (preprocess, shuffle, batch)
  eval_ds = deterministic_data.create_dataset(
    dataset_builder=dataset_builder, #as_dataset() method is needed
    split=eval_split,
    batch_dims=batch_dims,
    rng=None, 
    preprocess_fn=eval_preprocess_fn, 
    cache=False, 
    num_epochs=1, 
    shuffle=False,
    pad_up_to_batches="auto" 
  )

  return train_ds, eval_ds

