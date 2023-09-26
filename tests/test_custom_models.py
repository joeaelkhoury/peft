#!/usr/bin/env python3

# coding=utf-8
# Copyright 2023-present the HuggingFace Inc. team.
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
import copy
import itertools
import tempfile
import unittest

import torch
from parameterized import parameterized
from torch import nn
from transformers.pytorch_utils import Conv1D

from peft import AdaLoraConfig, IA3Config, LoraConfig, PeftModel, get_peft_model
from peft.tuners.tuners_utils import BaseTunerLayer

from .testing_common import PeftCommonTester
from .testing_utils import get_state_dict


# MLP is a vanilla FF network with only linear layers
# EmbConv1D has an embedding and a Conv1D layer
# Conv2D has a Conv2D layer
TEST_CASES = [
    ("Vanilla MLP 1", "MLP", LoraConfig, {"target_modules": "lin0"}),
    ("Vanilla MLP 2", "MLP", LoraConfig, {"target_modules": ["lin0"]}),
    ("Vanilla MLP 3", "MLP", LoraConfig, {"target_modules": ["lin1"]}),
    ("Vanilla MLP 4", "MLP", LoraConfig, {"target_modules": ["lin0", "lin1"]}),
    ("Vanilla MLP 5", "MLP", LoraConfig, {"target_modules": ["lin0"], "modules_to_save": ["lin1"]}),
    (
        "Vanilla MLP 6",
        "MLP",
        LoraConfig,
        {
            "target_modules": ["lin0"],
            "lora_alpha": 4,
            "lora_dropout": 0.1,
        },
    ),
    ("Embedding + transformers Conv1D 1", "EmbConv1D", LoraConfig, {"target_modules": ["conv1d"]}),
    ("Embedding + transformers Conv1D 2", "EmbConv1D", LoraConfig, {"target_modules": ["emb"]}),
    ("Embedding + transformers Conv1D 3", "EmbConv1D", LoraConfig, {"target_modules": ["emb", "conv1d"]}),
    ("Conv2d 1", "Conv2d", LoraConfig, {"target_modules": ["conv2d"]}),
    ("Conv2d 2", "Conv2d", LoraConfig, {"target_modules": ["conv2d", "lin0"]}),
]

TUNERS = ["lora", "ia3", "adalora"]
TARGETS = ["same", "different"]
TUNERS_AND_TARGETS = list(itertools.product(TUNERS, TARGETS))


class MLP(nn.Module):
    def __init__(self):
        super().__init__()
        self.lin0 = nn.Linear(10, 20)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.5)
        self.lin1 = nn.Linear(20, 2)
        self.sm = nn.LogSoftmax(dim=-1)

    def forward(self, X):
        X = X.float()
        X = self.lin0(X)
        X = self.relu(X)
        X = self.drop(X)
        X = self.lin1(X)
        X = self.sm(X)
        return X


class ModelEmbConv1D(nn.Module):
    def __init__(self):
        super().__init__()
        self.emb = nn.Embedding(100, 5)
        self.conv1d = Conv1D(1, 5)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)

    def forward(self, X):
        X = self.emb(X)
        X = self.conv1d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        return X


class ModelConv2D(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv2d = nn.Conv2d(5, 10, 3)
        self.relu = nn.ReLU()
        self.flat = nn.Flatten()
        self.lin0 = nn.Linear(10, 2)

    def forward(self, X):
        X = X.float().reshape(2, 5, 3, 3)
        X = self.conv2d(X)
        X = self.relu(X)
        X = self.flat(X)
        X = self.lin0(X)
        return X


class MockTransformerWrapper:
    """Mock class to behave like a transformers model.

    This is needed because the tests initialize the model by calling transformers_class.from_pretrained.

    """

    @classmethod
    def from_pretrained(cls, model_id):
        # set the seed so that from_pretrained always returns the same model
        torch.manual_seed(0)

        if model_id == "MLP":
            return MLP()

        if model_id == "EmbConv1D":
            return ModelEmbConv1D()

        if model_id == "Conv2d":
            return ModelConv2D()

        raise ValueError(f"model_id {model_id} not implemented")


class PeftCustomModelTester(unittest.TestCase, PeftCommonTester):
    """TODO"""

    transformers_class = MockTransformerWrapper

    def prepare_inputs_for_testing(self):
        X = torch.arange(90).view(9, 10).to(self.torch_device)
        return {"X": X}

    @parameterized.expand(TEST_CASES)
    def test_attributes_parametrized(self, test_name, model_id, config_cls, config_kwargs):
        self._test_model_attr(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_adapter_name(self, test_name, model_id, config_cls, config_kwargs):
        self._test_adapter_name(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_prepare_for_training_parametrized(self, test_name, model_id, config_cls, config_kwargs):
        # This test does not work with custom models because it assumes that
        # there is always a method get_input_embeddings that returns a layer
        # which does not need updates. Instead, a new test is added below that
        # checks that LoRA works as expected.
        pass

    @parameterized.expand(TEST_CASES)
    def test_save_pretrained(self, test_name, model_id, config_cls, config_kwargs):
        self._test_save_pretrained(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_from_pretrained_config_construction(self, test_name, model_id, config_cls, config_kwargs):
        self._test_from_pretrained_config_construction(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_merge_layers(self, test_name, model_id, config_cls, config_kwargs):
        config_kwargs = config_kwargs.copy()
        config_kwargs["init_lora_weights"] = False
        self._test_merge_layers(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_generate(self, test_name, model_id, config_cls, config_kwargs):
        # Custom models do not (necessarily) have a generate method, so this test is not performed
        pass

    @parameterized.expand(TEST_CASES)
    def test_generate_half_prec(self, test_name, model_id, config_cls, config_kwargs):
        # Custom models do not (necessarily) have a generate method, so this test is not performed
        pass

    @parameterized.expand(TEST_CASES)
    def test_training_custom_models(self, test_name, model_id, config_cls, config_kwargs):
        self._test_training(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_training_custom_models_layer_indexing(self, test_name, model_id, config_cls, config_kwargs):
        # At the moment, layer indexing only works when layer names conform to a specific pattern, which is not
        # guaranteed here. Therefore, this test is not performed.
        pass

    @parameterized.expand(TEST_CASES)
    def test_training_custom_models_gradient_checkpointing(self, test_name, model_id, config_cls, config_kwargs):
        self._test_training_gradient_checkpointing(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_inference_safetensors(self, test_name, model_id, config_cls, config_kwargs):
        self._test_inference_safetensors(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_peft_model_device_map(self, test_name, model_id, config_cls, config_kwargs):
        self._test_peft_model_device_map(model_id, config_cls, config_kwargs)

    @parameterized.expand(TEST_CASES)
    def test_only_params_are_updated(self, test_name, model_id, config_cls, config_kwargs):
        # An explicit test that when using LoRA on a custom model, only the LoRA parameters are updated during training
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model_before = copy.deepcopy(model)

        model.train()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.5)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            loss = y_pred.sum()
            loss.backward()
            optimizer.step()

        tol = 1e-4
        params_before = dict(model_before.named_parameters())
        params_after = dict(model.named_parameters())
        self.assertEqual(params_before.keys(), params_after.keys())
        for name, param_before in params_before.items():
            param_after = params_after[name]
            if ("lora_" in name) or ("modules_to_save" in name):
                # target_modules and modules_to_save _are_ updated
                self.assertFalse(torch.allclose(param_before, param_after, atol=tol, rtol=tol))
            else:
                self.assertTrue(torch.allclose(param_before, param_after, atol=tol, rtol=tol))

    @parameterized.expand(TEST_CASES)
    def test_parameters_after_loading_model(self, test_name, model_id, config_cls, config_kwargs):
        # An explicit test that when loading a trained model, the parameters are loaded correctly
        # see issue #808
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.train()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.5)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            loss = y_pred.sum()
            loss.backward()
            optimizer.step()

        tol = 1e-4
        params_before = get_state_dict(model)
        # note: no need to sanity check if parameters were updated at all, this
        # is already covered in the previous test

        with tempfile.TemporaryDirectory() as tmp_dirname:
            model.save_pretrained(tmp_dirname)
            model_from_pretrained = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
            model_from_pretrained = PeftModel.from_pretrained(model_from_pretrained, tmp_dirname)
            params_after = get_state_dict(model_from_pretrained)

            self.assertEqual(params_before.keys(), params_after.keys())
            for name, param_before in params_before.items():
                param_after = params_after[name]
                self.assertTrue(torch.allclose(param_before, param_after, atol=tol, rtol=tol))

    @parameterized.expand(TEST_CASES)
    def test_disable_adapters(self, test_name, model_id, config_cls, config_kwargs):
        X = self.prepare_inputs_for_testing()
        model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
        config = config_cls(
            base_model_name_or_path=model_id,
            **config_kwargs,
        )
        model = get_peft_model(model, config)
        model.eval()
        outputs_before = model(**X)

        model.train()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)

        # train at least 3 steps for all parameters to be updated (probably this is required because of symmetry
        # breaking of some LoRA layers that are initialized with constants)
        for _ in range(3):
            optimizer.zero_grad()
            y_pred = model(**X)
            loss = y_pred.sum()
            loss.backward()
            optimizer.step()

        model.eval()
        outputs_after = model(**X)

        with model.disable_adapter():
            outputs_disabled = model(**X)

        # check that after leaving the disable_adapter context, everything is enabled again
        outputs_enabled_after_disable = model(**X)

        self.assertFalse(torch.allclose(outputs_before, outputs_after))
        self.assertTrue(torch.allclose(outputs_before, outputs_disabled))
        self.assertTrue(torch.allclose(outputs_after, outputs_enabled_after_disable))

    @parameterized.expand(TEST_CASES)
    def test_disable_adapter_with_bias_warns(self, test_name, model_id, config_cls, config_kwargs):
        # When training biases in lora, disabling adapters does not reset the biases, so the output is not what users
        # might expect. Therefore, a warning should be given.

        # Note: We test only with custom models since they run really fast. There is really no point in testing the same
        # thing with decoder, encoder_decoder, etc.

        def run_with_disable(config_kwargs, bias):
            config_kwargs = config_kwargs.copy()
            config_kwargs["bias"] = bias
            model = self.transformers_class.from_pretrained(model_id).to(self.torch_device)
            config = config_cls(
                base_model_name_or_path=model_id,
                **config_kwargs,
            )
            peft_model = get_peft_model(model, config)
            with peft_model.disable_adapter():
                pass  # there is nothing to be done

        # check that bias=all and bias=lora_only give a warning with the correct message
        msg_start = "Careful, disabling adapter layers with bias configured to be"
        with self.assertWarns(UserWarning, msg=msg_start):
            run_with_disable(config_kwargs, bias="lora_only")
        with self.assertWarns(UserWarning, msg=msg_start):
            run_with_disable(config_kwargs, bias="all")

        # For bias=none, there is no warning. Unfortunately, AFAIK unittest has no option to assert that no warning is
        # given, therefore, we check that the unittest gives us an AssertionError if we check for a warning
        bias_warning_was_given = False
        try:
            with self.assertWarns(UserWarning) as cm:
                run_with_disable(config_kwargs, bias="none")
                # if we get here, it means there was no AssertionError, i.e. there are warnings -- let's check that they
                # are not related to the bias setting
                if any(warning.message.args[0].startswith(msg_start) for warning in cm.warnings):
                    bias_warning_was_given = True
        except AssertionError:
            # This is good, there was an AssertionError, i.e. there was no warning
            pass
        if bias_warning_was_given:
            # This is bad, there was a warning about the bias when there should not have been any.
            self.fail("There should be no warning when bias is set to 'none'")

    @parameterized.expand(TEST_CASES)
    def test_adding_multiple_adapters_with_bias_raises(self, test_name, model_id, config_cls, config_kwargs):
        self._test_adding_multiple_adapters_with_bias_raises(model_id, config_cls, config_kwargs)


class TestMultiRankAdapter(unittest.TestCase):
    """Tests related to multirank LoRA adapters"""

    def test_multirank(self):
        config_1 = LoraConfig(
            r=8,
            lora_alpha=8,
            init_lora_weights=False,
            target_modules=["lin0", "lin1"],
        )
        config_2 = LoraConfig(
            r=8,
            lora_alpha=8,
            init_lora_weights=False,
            target_modules=["lin0", "lin1"],
            rank_pattern={"lin0": 4},
            alpha_pattern={"lin0": 4},
        )

        # Add first adapter
        model = get_peft_model(MLP(), config_1, adapter_name="first")

        # Add second adapter
        model.add_adapter("second", config_2)

        # Extract current and expected ranks
        rank_current = model.lin0.lora_A["second"].weight.shape[0]
        rank_expected = config_2.rank_pattern["lin0"]

        self.assertTrue(rank_current == rank_expected, f"Rank {rank_current} is not equal to expected {rank_expected}")


class TestRepr(unittest.TestCase):
    """Tests related to the repr of adapted models"""

    def test_repr_lora_linear(self):
        config = LoraConfig(target_modules=["lin0"])
        model = get_peft_model(MLP(), config)
        print_output = repr(model.model.lin0)
        self.assertTrue(print_output.startswith("Linear"))
        self.assertTrue("in_features=10, out_features=20" in print_output)
        self.assertTrue("lora_A" in print_output)
        self.assertTrue("lora_B" in print_output)
        self.assertTrue("default" in print_output)

    def test_repr_lora_embedding(self):
        config = LoraConfig(target_modules=["emb"])
        model = get_peft_model(ModelEmbConv1D(), config)
        print_output = repr(model.model.emb)
        self.assertTrue(print_output.startswith("Embedding"))
        self.assertTrue("100, 5" in print_output)
        self.assertTrue("lora_embedding_A" in print_output)
        self.assertTrue("lora_embedding_B" in print_output)
        self.assertTrue("default" in print_output)

    def test_repr_lora_conv1d(self):
        config = LoraConfig(target_modules=["conv1d"])
        model = get_peft_model(ModelEmbConv1D(), config)
        print_output = repr(model.model.conv1d)
        self.assertTrue(print_output.startswith("Linear"))
        self.assertTrue("in_features=5, out_features=1" in print_output)
        self.assertTrue("lora_A" in print_output)
        self.assertTrue("lora_B" in print_output)
        self.assertTrue("default" in print_output)

    def test_repr_lora_conv2d(self):
        config = LoraConfig(target_modules=["conv2d"])
        model = get_peft_model(ModelConv2D(), config)
        print_output = repr(model.model.conv2d)
        self.assertTrue(print_output.startswith("Conv2d"))
        self.assertTrue("5, 10" in print_output)
        self.assertTrue("kernel_size=(3, 3)" in print_output)
        self.assertTrue("stride=(1, 1)" in print_output)
        self.assertTrue("lora_A" in print_output)
        self.assertTrue("lora_B" in print_output)
        self.assertTrue("default" in print_output)


class MultipleActiveAdaptersTester(unittest.TestCase):
    """
    A test class to test the functionality of multiple active adapters.

    This is not specifically tied to custom models, it's just easy to test here and testing it on all types of models
    would be overkill.
    """

    def setUp(self):
        super().setUp()
        self.configs = {
            "lora": {"class": LoraConfig, "kwargs": {"target_modules": ["lin0"], "init_lora_weights": False}},
            "ia3": {
                "class": IA3Config,
                "kwargs": {
                    "target_modules": ["lin0", "lin1"],
                    "feedforward_modules": ["lin0"],
                    "init_ia3_weights": False,
                },
            },
            "adalora": {"class": AdaLoraConfig, "kwargs": {"target_modules": ["lin0"], "init_lora_weights": False}},
        }

    def prepare_inputs_for_testing(self):
        X = torch.arange(90).view(9, 10)
        return {"X": X}

    def get_adapter_config(self, tuner_method, targets):
        if targets == "same":
            return self.configs[tuner_method]
        config = self.configs[tuner_method].copy()
        kwargs = config["kwargs"]
        for key, value in kwargs.items():
            new_targets = []
            if not isinstance(value, list):
                continue
            for target in value:
                new_targets.append("lin1" if target == "lin0" else "lin0")
            kwargs[key] = new_targets
        return config

    def set_multiple_active_adapters(self, model, adapter_names):
        for module in model.modules():
            if isinstance(module, BaseTunerLayer):
                module.set_adapter(adapter_names)

    @parameterized.expand(TUNERS_AND_TARGETS)
    def test_multiple_active_adapters_forward(self, tuner_method, targets):
        model = MLP()
        model.eval()
        X = self.prepare_inputs_for_testing()

        config_1 = self.get_adapter_config(tuner_method, targets)
        config_2 = self.get_adapter_config(tuner_method, targets)
        config_cls = config_1["class"]
        config_1 = config_cls(**config_1["kwargs"])
        config_2 = config_cls(**config_2["kwargs"])

        peft_model = get_peft_model(model, config_1, adapter_name="adapter_1")
        peft_model.add_adapter("adapter_2", config_2)

        # set adapter_1
        peft_model.set_adapter("adapter_1")
        adapter_1_output = peft_model(**X)

        # set adapter_2
        peft_model.set_adapter("adapter_2")
        adapter_2_output = peft_model(**X)

        # set ["adapter_1", "adapter_2"]
        self.set_multiple_active_adapters(peft_model, ["adapter_1", "adapter_2"])
        combined_output = peft_model(**X)

        self.assertFalse(torch.allclose(adapter_1_output, adapter_2_output))
        self.assertFalse(torch.allclose(adapter_1_output, combined_output))
        self.assertFalse(torch.allclose(adapter_2_output, combined_output))

        if tuner_method == "lora":
            # create a weighted adapter combining both adapters and check that
            # its output is same as setting multiple active adapters
            peft_model.add_weighted_adapter(
                ["adapter_1", "adapter_2"], [1.0, 1.0], "new_combined_adapter", combination_type="cat"
            )
            peft_model.set_adapter("new_combined_adapter")
            new_combined_output = peft_model(**X)
            self.assertTrue(torch.allclose(new_combined_output, combined_output))

    @parameterized.expand(TUNERS_AND_TARGETS)
    def test_multiple_active_adapters_merge_and_unmerge(self, tuner_method, targets):
        model = MLP()
        model.eval()
        X = self.prepare_inputs_for_testing()
        base_output = model(**X)

        config_1 = self.get_adapter_config(tuner_method, targets)
        config_2 = self.get_adapter_config(tuner_method, targets)
        config_cls = config_1["class"]
        config_1 = config_cls(**config_1["kwargs"])
        config_2 = config_cls(**config_2["kwargs"])

        peft_model = get_peft_model(model, config_1, adapter_name="adapter_1")
        peft_model.add_adapter("adapter_2", config_2)

        # set ["adapter_1", "adapter_2"]
        self.set_multiple_active_adapters(peft_model, ["adapter_1", "adapter_2"])
        combined_output = peft_model(**X)

        peft_model.merge_adapter()
        merged_combined_output = peft_model(**X)
        self.assertTrue(torch.allclose(merged_combined_output, combined_output))

        peft_model.unmerge_adapter()

        with peft_model.disable_adapter():
            disabled_adapter_output = peft_model(**X)

        self.assertTrue(torch.allclose(disabled_adapter_output, base_output))


class RequiresGradTester(unittest.TestCase):
    """Test that requires_grad is set correctly in specific circumstances

    # See issue #899.

    This is not specifically tied to custom models, it's just easy to test here and testing it on all types of models
    would be overkill.

    """

    def test_requires_grad_modules_to_save_default(self):
        config = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config)

        self.assertTrue(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.bias.requires_grad)

    def test_requires_grad_modules_to_save_disabling(self):
        config = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config)

        # when disabling the adapter, the original module's grad should be enabled and vice versa
        peft_model.disable_adapter_layers()
        self.assertFalse(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertTrue(peft_model.model.lin1.original_module.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.original_module.bias.requires_grad)

        # when re-enabling the adapter, the original module's grad should be disabled and vice versa
        peft_model.enable_adapter_layers()
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.bias.requires_grad)

        # when using the disable_adapter context, the original module's grad should be enabled and vice versa
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
            self.assertFalse(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
            self.assertTrue(peft_model.model.lin1.original_module.weight.requires_grad)
            self.assertTrue(peft_model.model.lin1.original_module.bias.requires_grad)

        # after context is exited, return to the previous state
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.original_module.bias.requires_grad)

    def test_requires_grad_modules_to_save_multiple_adapters(self):
        config0 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin0"], modules_to_save=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.adapter1.bias.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.adapter1.bias.requires_grad)

        # set config1 as active, should lead to adapter1 requiring grad
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin1.modules_to_save.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.modules_to_save.default.bias.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.adapter1.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.modules_to_save.adapter1.bias.requires_grad)

    def test_requires_grad_lora_different_targets(self):
        # test two different LoRA adapters that target different modules
        config0 = LoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_A.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_B.adapter1.weight.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_A.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_B.adapter1.weight.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_A.adapter1.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_B.adapter1.weight.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
            self.assertFalse(peft_model.model.lin1.lora_A.adapter1.weight.requires_grad)
            self.assertFalse(peft_model.model.lin1.lora_B.adapter1.weight.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_A.adapter1.weight.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_B.adapter1.weight.requires_grad)

    def test_requires_grad_lora_same_targets(self):
        # same as previous test, except that LoRA adapters target the same layer
        config0 = LoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = LoraConfig(target_modules=["lin0"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_A.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.adapter1.weight.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_A.adapter1.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.adapter1.weight.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_A.adapter1.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.adapter1.weight.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_A.adapter1.weight.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.adapter1.weight.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.weight.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_A.adapter1.weight.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.adapter1.weight.requires_grad)

    def test_requires_grad_ia3_different_targets(self):
        # test two different IA3 adapters that target different modules
        config0 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = IA3Config(target_modules=["lin1"], feedforward_modules=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertFalse(peft_model.model.lin1.ia3_l.adapter1.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertFalse(peft_model.model.lin1.ia3_l.adapter1.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertTrue(peft_model.model.lin1.ia3_l.adapter1.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
            self.assertFalse(peft_model.model.lin1.ia3_l.adapter1.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertTrue(peft_model.model.lin1.ia3_l.adapter1.requires_grad)

    def test_requires_grad_ia3_same_targets(self):
        # same as previous test, except that IA3 adapters target the same layer
        config0 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = IA3Config(target_modules=["lin0"], feedforward_modules=["lin1"])
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.ia3_l.adapter1.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.ia3_l.adapter1.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.ia3_l.adapter1.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.ia3_l.adapter1.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.ia3_l.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.ia3_l.adapter1.requires_grad)

    def test_requires_grad_adalora_different_targets(self):
        # test two different AdaLora adapters that target different modules
        config0 = AdaLoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = AdaLoraConfig(target_modules=["lin1"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_A.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_B.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_E.adapter1.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_A.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_B.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin1.lora_E.adapter1.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_A.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_B.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_E.adapter1.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
            self.assertFalse(peft_model.model.lin1.lora_A.adapter1.requires_grad)
            self.assertFalse(peft_model.model.lin1.lora_B.adapter1.requires_grad)
            self.assertFalse(peft_model.model.lin1.lora_E.adapter1.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_A.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_B.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin1.lora_E.adapter1.requires_grad)

    def test_requires_grad_adalora_same_targets(self):
        # same as previous test, except that AdaLora adapters target the same layer
        config0 = AdaLoraConfig(target_modules=["lin0"])
        peft_model = get_peft_model(MLP(), config0)

        config1 = AdaLoraConfig(target_modules=["lin0"], inference_mode=True)
        peft_model.add_adapter("adapter1", config1)

        # active adapter is still "default"
        self.assertTrue(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_A.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.adapter1.requires_grad)

        # set config0 as active, should not change anything
        peft_model.set_adapter("default")
        self.assertTrue(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_A.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.adapter1.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.adapter1.requires_grad)

        # change activate adapter to adapter1
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_A.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_E.adapter1.requires_grad)

        # disable all adapters
        with peft_model.disable_adapter():
            self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_A.adapter1.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_B.adapter1.requires_grad)
            self.assertFalse(peft_model.model.lin0.lora_E.adapter1.requires_grad)

        # after context is exited, return to the previous state
        peft_model.set_adapter("adapter1")
        self.assertFalse(peft_model.model.lin0.lora_A.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_B.default.requires_grad)
        self.assertFalse(peft_model.model.lin0.lora_E.default.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_A.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_B.adapter1.requires_grad)
        self.assertTrue(peft_model.model.lin0.lora_E.adapter1.requires_grad)
