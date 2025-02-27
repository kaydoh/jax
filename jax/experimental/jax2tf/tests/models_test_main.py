# Copyright 2022 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""Tests for converting Flax models using jax2tf.

Note: we do not use actual absl tests for this since we do not want this to run
as part of our CI. Instead, we mainly use these tests to generate the markdown
file.

Run this as follows:

```
# Run all converters on all suites and regenerate the table.
python3 models_test_main.py

# Run only CNN and Resnet50 with the TFjs converter and don't write Markdown.
python3 models_test_main.py \
    --examples=flax/cnn,flax/resnet50 \
    --converters=jax2tf_noxla_tfjs \
    --write_markdown=False
```
"""
from absl import app
from absl import flags

import functools
import numpy as np
import os
import re
import jax

from jax.config import config

config.parse_flags_with_absl()

from jax._src import dtypes
import jax.numpy as jnp

import datetime
from typing import Dict, List, Sequence, Tuple

from pyglib import gfile
from pyglib import resources

import tensorflow as tf

from jax.experimental.jax2tf.tests.model_harness import ALL_HARNESSES
from jax.experimental.jax2tf.tests.converters import ALL_CONVERTERS

flags.DEFINE_list("converters", [x.name for x in ALL_CONVERTERS],
                  "Which converters to test.")

flags.DEFINE_list("examples", [],
                  ("List of examples to test, e.g.: 'flax/mnist,flax/seq2seq'. "
                   "If empty, will test all examples."))

flags.DEFINE_bool(
    "write_markdown", True,
    "If true, write results as Markdown. Otherwise, only output to stdout.")

flags.DEFINE_bool(
    "fail_on_error", False,
    ("If true, exit with an error when a conversion fails. Useful for "
     "debugging because it will show the entire stack trace."))

FLAGS = flags.FLAGS


def _write_markdown(results: Dict[str, List[Tuple[str, str,]]]) -> None:
  """Writes all results to Markdown file."""
  table_lines = []
  converters = FLAGS.converters

  table_lines.append("| Example | " + " ".join([f"{c} |" for c in converters]))
  table_lines.append("|" + (" --- |" * (len(converters) + 1)))

  error_lines = []

  def _header2anchor(header):
    header = header.lower().replace(" ", "-")
    header = "#" + re.sub(r"[^a-zA-Z0-9_-]+", "", header)
    return header

  for example_name, converter_results in results.items():
    # Make sure the converters are in the right order.
    assert [c[0] for c in converter_results] == converters
    line = f"| {example_name} |"
    example_error_lines = []
    for converter_name, error_msg in converter_results:
      if not error_msg:
        line += " YES |"
      else:
        error_header = (f"Example: `{example_name}` | "
                        f"Converter: `{converter_name}`")
        example_error_lines.append("### " + error_header)
        example_error_lines.append(f"```\n{error_msg}\n```")
        example_error_lines.append("[Back to top](#summary-table)\n")
        line += f" [NO]({_header2anchor(error_header)}) | "
    table_lines.append(line)

    if example_error_lines:
      error_lines.append(f"## `{example_name}`")
      error_lines.extend(example_error_lines)

  # TODO(marcvanzee): This is somewhat brittle, consider rewriting it.
  g3doc_path = "../g3doc"
  output_path = os.path.join(g3doc_path, "convert_models_results.md")
  template_path = output_path + ".template"

  template = resources.GetResource(template_path).decode("utf-8")
  template = template.replace("{{generation_date}}", str(datetime.date.today()))
  template = template.replace("{{table}}", "\n".join(table_lines))
  template = template.replace("{{errors}}", "\n".join(error_lines))

  if (workdir := "BUILD_WORKING_DIRECTORY") in os.environ:
    os.chdir(os.path.dirname(os.environ[workdir]))

  with gfile.Open(output_path, "w") as f:
    f.write(template)

  print("Written converter results to", output_path)


def _crop_convert_error(msg: str) -> str:
  msg = msg.replace("\\n", "\n").replace("\\t", "\t")

  tflite_msg = "Some ops are not supported by the native TFLite runtime"
  if tflite_msg in msg:
    # Filters out only the missing ops.
    missing_ops = msg.split("\nDetails:\n")[1].split("\n\n")[0]
    return tflite_msg + "\n" + missing_ops

  tfjs_msg = "Some ops in the model are custom ops"
  if tfjs_msg in msg:
    return msg[msg.index(tfjs_msg):]

  return msg


def _get_random_data(x: jnp.ndarray) -> np.ndarray:
  dtype = dtypes.canonicalize_dtype(x.dtype)
  if np.issubdtype(dtype, np.integer):
    return np.random.randint(0, 100, size=x.shape, dtype=dtype)
  elif np.issubdtype(dtype, np.floating):
    return np.array(np.random.uniform(size=x.shape), dtype=dtype)
  elif dtype == bool:
    return np.random.choice(a=[False, True], size=x.shape)
  else:
    raise ValueError(f"Unsupported dtype for numerical comparison: {dtype}")


def test_converters():
  """Tests all converters and write output."""
  def _exit_if_empty(x, name):
    if not x:
      print(f"WARNING: No {name} found to test! Exiting")
      exit()

  def _maybe_reraise(e):
    if FLAGS.fail_on_error:
      raise e

  def _format(e):
    # InvalidArgumentError output is shown differently.
    msg = str(e) if repr(e).strip() == "InvalidArgumentError()" else repr(e)
    msg = msg.replace("\n\n", "\n")  # Output newlines correctly.
    return msg

  converters = list(
      filter(lambda x: x.name in FLAGS.converters, ALL_CONVERTERS))
  _exit_if_empty(converters, "converters")

  harnesses_to_test = {
      name: fn for name, fn in ALL_HARNESSES.items()
      if not FLAGS.examples or name in FLAGS.examples
  }
  _exit_if_empty(harnesses_to_test, "harness")

  results = {}
  for name, harness_fn in harnesses_to_test.items():
    harness = harness_fn(name)  # This will create the variables.
    np_assert_allclose = functools.partial(
        np.testing.assert_allclose, rtol=harness.rtol)
    converter_results = []
    for converter in converters:
      print(f"===== Testing example {harness.name}, "
            f"Converter {converter.name}")
      error_msg = ""
      try:
        apply_tf = converter.convert_fn(harness)
        print("=== Conversion OK!")
      except Exception as e:  # pylint: disable=broad-except
        error_msg = "Conversion error\n" + _crop_convert_error(repr(e))
        print(f"=== {error_msg}")
        _maybe_reraise(e)
      else:
        if not converter.compare_numerics:
          print("=== Skipping numerical comparison.")
        else:
          xs = [jax.tree_util.tree_map(_get_random_data, x) for x in harness.inputs]
          jax_result = harness.apply_with_vars(*xs)
          try:
            tf_result = apply_tf(*xs)
            jax.tree_map(np_assert_allclose, jax_result, tf_result)
            print("=== Numerical comparison OK!")
          except AssertionError as e:
            error_msg = "Numerical comparison error:\n" + _format(e)
            print("===", error_msg)
            _maybe_reraise(e)
          except (RuntimeError, tf.errors.InvalidArgumentError) as e:
            # TF sometimes throws a runtime error during inference.
            error_msg = _format(e)
            print("=== ", error_msg)
            _maybe_reraise(e)

      converter_results.append((converter.name, error_msg))
    results[harness.name] = converter_results

  if FLAGS.write_markdown:
    _write_markdown(results)
  else:
    print("=== NOT writing results to Markdown.")


def main(argv: Sequence[str]) -> None:
  if len(argv) > 1:
    raise app.UsageError("Too many command-line arguments.")

  test_converters()


if __name__ == "__main__":
  app.run(main)
