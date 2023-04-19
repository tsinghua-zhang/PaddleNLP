# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

from typing import Any, Dict, Optional

import paddle
from paddle.static import InputSpec

from ..transformers.model_outputs import (
    CausalLMOutputWithCrossAttentions,
    MaskedLMOutput,
    MultipleChoiceModelOutput,
    SequenceClassifierOutput,
)
from .prompt_utils import signature
from .template import PrefixTemplate, Template
from .verbalizer import Verbalizer


class PromptModelForSequenceClassification(paddle.nn.Layer):
    """
    PromptModel for classification tasks.
    """

    def __init__(
        self,
        model: paddle.nn.Layer,
        template: Template,
        verbalizer: Optional[Verbalizer] = None,
        freeze_plm: bool = False,
        freeze_dropout: bool = False,
    ):
        super(PromptModelForSequenceClassification, self).__init__()
        self.plm = model
        self.template = template
        self.verbalizer = verbalizer
        self.freeze_plm = freeze_plm
        self.freeze_dropout = freeze_dropout
        if self.freeze_plm:
            for param in self.plm.parameters():
                param.stop_gradient = True
            if self.freeze_dropout:
                self.plm.eval()
        self.forward_keys = signature(self.plm.forward)
        self._mask_token_id = self.template.tokenizer.mask_token_id
        self._pad_token_id = self.template.tokenizer.pad_token_id
        if isinstance(self.template, PrefixTemplate):
            self.plm = self.template.process_model(self.plm)
            self.forward_keys.append("past_key_values")

    def forward(
        self,
        input_ids: paddle.Tensor,
        token_type_ids: Optional[paddle.Tensor] = None,
        position_ids: Optional[paddle.Tensor] = None,
        attention_mask: Optional[paddle.Tensor] = None,
        masked_positions: Optional[paddle.Tensor] = None,
        soft_token_ids: Optional[paddle.Tensor] = None,
        encoder_ids: Optional[paddle.Tensor] = None,
        labels: Optional[paddle.Tensor] = None,
        return_dict: Optional[bool] = None,
        **kwargs: Dict[str, Any]
    ):
        return_dict = return_dict if return_dict is not None else False
        return_hidden_states = kwargs.get("return_hidden_states", False)
        input_dict = {
            "input_ids": input_ids,
            "token_type_ids": token_type_ids,
            "position_ids": position_ids,
            "masked_positions": masked_positions,
            "soft_token_ids": soft_token_ids,
            "attention_mask": attention_mask,
            "encoder_ids": encoder_ids,
            **kwargs,
        }
        input_dict = self.template.process_batch(input_dict)
        input_dict = {**input_dict, **kwargs}
        model_inputs = {k: input_dict[k] for k in input_dict if k in self.forward_keys}
        if "masked_positions" in model_inputs:
            model_inputs.pop("masked_positions")
        model_outputs = self.plm(**model_inputs, return_dict=True)
        if isinstance(model_outputs, MaskedLMOutput):
            if self.verbalizer is not None:
                logits = self.verbalizer.process_outputs(model_outputs.logits, input_dict["masked_positions"])
                num_labels = len(self.verbalizer.label_words)
            else:
                raise Exception("Verbalizer is required when model uses the MaskedLM head")
        elif isinstance(model_outputs, SequenceClassifierOutput):
            logits = model_outputs.logits
            num_labels = self.plm.num_labels if self.plm.num_labels is not None else self.plm.num_labels
        elif isinstance(model_outputs, MultipleChoiceModelOutput):
            logits = model_outputs.logits
            num_labels = -1
        else:
            raise Exception(f"Model type not support yet: {type(model_outputs)}")

        loss = None
        if labels is not None:
            if num_labels == 1:
                loss_fct = paddle.nn.MSELoss()
                loss = loss_fct(logits, labels)
            elif num_labels > 0 and (labels.dtype == paddle.int64 or labels.dtype == paddle.int32):
                loss_fct = paddle.nn.CrossEntropyLoss()
                loss = loss_fct(logits.reshape((-1, num_labels)), labels.reshape((-1,)))
            else:
                loss_fct = paddle.nn.BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,)
            if return_hidden_states:
                output = output + (model_outputs.logits,)
            if loss is not None:
                return (loss,) + output
            if isinstance(output, (list, tuple)) and len(output) == 1:
                output = output[0]
            return output

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=model_outputs.logits,
        )

    def prompt_parameters(self):
        """
        Get the parameters of template and verbalizer.
        """
        params = [p for p in self.template.parameters()]
        if self.verbalizer is not None:
            params += [p for p in self.verbalizer.parameters()]
        return params

    def get_input_spec(self):
        template_keywords = self.template.extract_template_keywords(self.template.prompt)
        input_spec = [
            InputSpec(shape=[None, None], dtype="int64", name="input_ids"),
            InputSpec(shape=[None, None], dtype="int64", name="token_type_ids"),
            InputSpec(shape=[None, None], dtype="int64", name="position_ids"),
            InputSpec(shape=[None, None, None, None], dtype="float32", name="attention_mask"),
        ]
        if "mask" in template_keywords:
            input_spec.append(InputSpec(shape=[None], dtype="int64", name="masked_positions"))
        if "soft" in template_keywords:
            # Add placeholder for argument `masked_positions` if not exists.
            if "mask" not in template_keywords:
                input_spec.append(None)
            input_spec.append(InputSpec(shape=[None, None], dtype="int64", name="soft_token_ids"))
            if "encoder" in template_keywords:
                input_spec.append(InputSpec(shape=[None, None], dtype="int64", name="encoder_ids"))
        return input_spec


class PromptModelForGeneration(paddle.nn.Layer):
    """
    PromptModel for classification tasks.
    """

    def __init__(
        self,
        model: paddle.nn.Layer,
        template: Template,
        freeze_plm: bool = False,
        freeze_dropout: bool = False,
    ):
        super(PromptModelForGeneration, self).__init__()
        self.plm = model
        self.template = template
        self.freeze_plm = freeze_plm
        self.freeze_dropout = freeze_dropout
        if self.freeze_plm:
            for param in self.plm.parameters():
                param.stop_gradient = True
            if self.freeze_dropout:
                self.plm.eval()
        self.forward_keys = signature(self.plm.forward)
        self._mask_token_id = self.template.tokenizer.mask_token_id
        self._pad_token_id = self.template.tokenizer.pad_token_id
        if not isinstance(self.template, PrefixTemplate):
            raise TypeError(f"{self.__class__.__name__} is not compatible with {self.template.__class__.__name__} ")
        self.plm = self.template.process_model(self.plm)
        self.forward_keys.append("past_key_values")
        self.base_model_prepare_inputs_for_generation = self.plm.prepare_inputs_for_generation

    def forward(
        self,
        input_ids: paddle.Tensor,
        token_type_ids: Optional[paddle.Tensor] = None,
        position_ids: Optional[paddle.Tensor] = None,
        soft_token_ids: Optional[paddle.Tensor] = None,
        encoder_ids: Optional[paddle.Tensor] = None,
        labels: Optional[paddle.Tensor] = None,
        return_dict: Optional[bool] = None,
        **kwargs: Dict[str, Any]
    ):
        return_dict = return_dict if return_dict is not None else False
        if soft_token_ids is None:
            outputs = self.plm(input_ids)
            return outputs

        return_hidden_states = kwargs.get("return_hidden_states", False)
        input_dict = {
            "input_ids": input_ids,
            "token_type_ids": token_type_ids,
            "position_ids": position_ids,
            "soft_token_ids": soft_token_ids,
            "encoder_ids": encoder_ids,
            "labels": labels,
            **kwargs,
        }
        input_dict = self.template.process_batch(input_dict)
        input_dict = {**input_dict, **kwargs}
        model_inputs = {k: input_dict[k] for k in input_dict if k in self.forward_keys}
        if "cache" in self.forward_keys:
            model_inputs["cache"] = []
            for i in range(len(model_inputs["past_key_values"])):
                from paddlenlp.transformers.gpt.modeling import MultiHeadAttention

                model_inputs["cache"].append(
                    MultiHeadAttention.Cache(
                        k=model_inputs["past_key_values"][i][0], v=model_inputs["past_key_values"][i][1]
                    )
                )
            model_inputs.pop("past_key_values")
        model_inputs.pop("labels")
        model_outputs = self.plm(**model_inputs, return_dict=True, use_cache=True)
        logits = model_outputs.logits

        loss = None
        if labels is not None:
            shift_labels = labels[..., 1:]
            shift_logits = logits[..., : shift_labels.shape[1], :]
            loss_fct = paddle.nn.CrossEntropyLoss(ignore_index=-100, reduction="mean")
            loss = loss_fct(shift_logits.reshape((-1, shift_logits.shape[-1])), shift_labels.reshape((-1,))).reshape(
                [-1]
            )

        if not return_dict:
            output = (logits,)
            if return_hidden_states:
                output = output + (model_outputs.logits,)
            if loss is not None:
                return (loss,) + output
            if isinstance(output, (list, tuple)) and len(output) == 1:
                output = output[0]
            return output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=logits,
            past_key_values=model_outputs.past_key_values,
            hidden_states=model_outputs.logits,
        )

    def generate(self, model_kwargs, **kwargs):
        self.plm.prepare_inputs_for_generation = self.prepare_inputs_for_generation
        generated_tokens = self.plm.generate(**model_kwargs, **kwargs)
        return generated_tokens

    def prepare_inputs_for_generation(self, input_ids, use_cache=False, cache=None, **kwargs):
        model_kwargs = self.base_model_prepare_inputs_for_generation(input_ids, cache=None, **kwargs)
        model_kwargs["soft_token_ids"] = kwargs.get("soft_token_ids", None)
        model_kwargs["token_type_ids"] = kwargs.get("token_type_ids", None)
        model_kwargs["encoder_ids"] = kwargs.get("encoder_ids", None)
        len_dif = len(model_kwargs["token_type_ids"][0]) - len(model_kwargs["soft_token_ids"][0])
        for _ in range(len_dif):
            model_kwargs["soft_token_ids"] = paddle.concat(
                [model_kwargs["soft_token_ids"], paddle.to_tensor([[0]])], axis=1
            )
        input_dict = self.template.process_batch(model_kwargs)
        model_inputs = {k: input_dict[k] for k in input_dict if k in self.forward_keys}
        if "cache" in self.forward_keys:
            model_inputs["cache"] = []
            for i in range(len(model_inputs["past_key_values"])):
                from paddlenlp.transformers.gpt.modeling import MultiHeadAttention

                model_inputs["cache"].append(
                    MultiHeadAttention.Cache(
                        k=model_inputs["past_key_values"][i][0], v=model_inputs["past_key_values"][i][1]
                    )
                )
            model_inputs.pop("past_key_values")
        model_inputs["use_cache"] = True
        model_inputs["return_dict"] = True

        return model_inputs
