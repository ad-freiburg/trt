import collections
import math
import os
import pickle
import pprint
from typing import Union, List, Optional, Iterable

import numpy as np
import torch
from tqdm import tqdm

from trt.api.utils import download_model, get_device_info, split
from trt.model import transformer
from trt.utils import common, config, io, inference, nlp, tokenization_repair

os.environ["TOKENIZERS_PARALLELISM"] = "false"

__all__ = ["ModelInfo", "get_available_models", "TokenizationRepairer"]

ModelInfo = collections.namedtuple("ModelInfo", ["name", "description"])


def get_available_models() -> List[ModelInfo]:
    return [
        ModelInfo(
            name="eo_arxiv_with_errors",
            description="best overall model, use this for text that might have OCR or spelling errors (default)"
        ),
        ModelInfo(
            name="eo_medium_arxiv_with_errors",
            description="compromise between eo_arxiv_with_errors and eo_small_arxiv_with_errors, "
                        "faster than eo_arxiv_with_errors but less accurate, "
                        "slower than eo_small_arxiv_with_errors but more accurate"
        ),
        ModelInfo(
            name="eo_small_arxiv_with_errors",
            description="smallest and fastest, but also the least accurate model, "
                        "use this when you want to repair text with few tokenization errors and "
                        "little to no OCR or spelling errors fast"
        )
    ]


StringInputOutput = Union[str, List[str]]


class TokenizationRepairer:
    @staticmethod
    def from_pretrained(
            model: str = get_available_models()[0].name,
            device: Union[str, int] = "cuda",
            cache_dir: Optional[str] = None,
            force_download: bool = False
    ) -> "TokenizationRepairer":
        assert any(model == m.name for m in get_available_models()), \
            f"model {model} does not match any of the available models:\n{pprint.pformat(get_available_models())}"

        if cache_dir is None:
            cache_dir = os.environ.get(
                "TOKENIZATION_REPAIR_CACHE_DIR",
                os.path.join(os.path.dirname(__file__), ".cache")
            )

        logger = common.get_logger("DOWNLOAD")
        model_dir = download_model(model, cache_dir, force_download, logger)

        return TokenizationRepairer(model_dir, device)

    @staticmethod
    def from_experiment(
            experiment_dir: str,
            device: Union[str, int] = "cuda"
    ) -> "TokenizationRepairer":
        return TokenizationRepairer(experiment_dir, device)

    @property
    def model_name(self) -> str:
        return self.cfg.model.name

    def __init__(self,
                 model_dir: str,
                 device: Union[str, int]) -> None:
        self.logger = common.get_logger("TOKENIZATION_REPAIR")

        if device != "cpu" and not torch.cuda.is_available():
            self.logger.info(f"could not find a GPU, using CPU as fallback option")
            device = "cpu"

        self.device = torch.device(device)
        self.logger.info(f"running tokenization repair on device {get_device_info(self.device)}")

        self.cfg = config.Config.from_yaml(os.path.join(model_dir, "config.yaml"))
        self.logger.debug(f"loaded model config:\n{self.cfg}")

        self.model = transformer.get_model_from_config(self.cfg.model, self.device)
        best_checkpoint_path = io.glob_safe(os.path.join(model_dir, "checkpoints", "*-checkpoint-best.pt"))[0]
        best_checkpoint = io.load_checkpoint(best_checkpoint_path)
        self.model.load_state_dict(best_checkpoint["model_state_dict"])
        self.model.eval()
        for param in self.model.parameters():
            param.requires_grad = False

        # set default inference kwargs
        self.inference_kwargs = {
            "temperature": 1.0,
            "temperature_no_spaces": 1.0,
            "thresholds_and_default": None,
            "thresholds_and_default_no_spaces": None
        }

        # check if the corresponding inference pickle files are in the model dir, if so, load them
        temperature_path = os.path.join(model_dir, "temperature.pkl")
        temperature_no_spaces_path = os.path.join(model_dir, "temperature_no_spaces.pkl")
        thresholds_and_default_path = os.path.join(model_dir, "thresholds_and_default.pkl")
        thresholds_and_default_no_spaces_path = os.path.join(model_dir, "thresholds_and_default_no_spaces.pkl")
        if os.path.exists(temperature_path):
            with open(temperature_path, "rb") as tf:
                temp = pickle.load(tf)
                self.inference_kwargs["temperature"] = temp
            self.logger.debug(f"found temperature file: setting temperature to {temp}")
        if os.path.exists(temperature_no_spaces_path):
            with open(temperature_no_spaces_path, "rb") as tf:
                temp_no_spaces = pickle.load(tf)
                self.inference_kwargs["temperature_no_spaces"] = temp_no_spaces
            self.logger.debug(f"found temperature (no spaces) file: setting temperature to {temp_no_spaces}")
        if os.path.exists(thresholds_and_default_path):
            with open(thresholds_and_default_path, "rb") as tf:
                thresholds_and_default = pickle.load(tf)
                self.inference_kwargs["thresholds_and_default"] = thresholds_and_default
            self.logger.debug(f"found thresholds_and_default file: setting thresholds and default to "
                              f"{thresholds_and_default}")
        if os.path.exists(thresholds_and_default_no_spaces_path):
            with open(thresholds_and_default_no_spaces_path, "rb") as tf:
                thresholds_and_default_no_spaces = pickle.load(tf)
                self.inference_kwargs["thresholds_and_default_no_spaces"] = thresholds_and_default_no_spaces
            self.logger.debug(f"found thresholds_and_default (no spaces) file: setting thresholds and default to "
                              f"{thresholds_and_default_no_spaces}")

        self.max_length = self.model.encoder.config.max_num_embeddings - 2  # - 2 because of bos and eos tokens
        self.overlap = math.ceil(self.max_length / 2)
        self.half_overlap_floor = 0  # to enable half_overlap_merging set this to math.floor(self.overlap / 2)

    def _merge_inference_results(
            self,
            inference_results: List[inference.SequenceClassificationInferenceResult],
            start_positions: List[int]
    ) -> inference.InferenceResult:
        assert (
                len(inference_results) > 0
                and len(inference_results) == len(start_positions)
                and sorted(start_positions) == start_positions
        )
        if len(inference_results) == 1:
            return inference_results[0]

        num_merged_predictions = 2 + start_positions[-1] + self.max_length
        merged_logits = np.zeros((num_merged_predictions, len(inference_results[0].logits[0])), dtype=float)
        num_logit_updates = np.zeros((num_merged_predictions, 1), dtype=int)
        for i, (start_pos, ir) in enumerate(zip(start_positions, inference_results)):
            from_pos = start_pos + 1
            if i > 0:
                from_pos += self.half_overlap_floor
            to_pos = start_pos + 1 + self.max_length
            if i < len(inference_results) - 1:
                to_pos -= self.half_overlap_floor
            assert len(ir.logits) == self.max_length + 2
            merged_logits[from_pos:to_pos] += ir.logits[from_pos - start_pos:to_pos - start_pos]
            num_logit_updates[from_pos:to_pos] += 1
        num_logit_updates[0] = 1  # bos token updates (to avoid dividing by zero)
        num_logit_updates[-1] = 1  # eos token updates (to avoid dividing by zero)
        assert np.all(num_logit_updates > 0)  # make sure every position got an update
        merged_logits /= num_logit_updates
        merged_predictions = np.argmax(merged_logits, axis=-1)
        return inference.SequenceClassificationInferenceResult(merged_predictions.tolist(), merged_logits.tolist())

    @torch.inference_mode()
    def _repair_text_raw(
            self,
            inputs: List[str],
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = False
    ) -> List[inference.InferenceResult]:
        # maybe sort inputs
        input_indices_and_lengths = [(i, len(ipt)) for i, ipt in enumerate(inputs)]
        if sort_by_length:
            input_indices_and_lengths = sorted(input_indices_and_lengths, key=lambda e: e[1], reverse=True)

        sum_lengths = sum(length for _, length in input_indices_and_lengths)
        pbar = tqdm(
            range(0, len(inputs), batch_size),
            total=sum_lengths,
            ascii=True,
            leave=False,
            disable=not show_progress,
            unit="char"
        )

        all_inference_results: List[inference.InferenceResult] = [None] * len(inputs)  # type: ignore
        for i, batch_idx in enumerate(pbar):
            batch_indices_and_lengths = input_indices_and_lengths[batch_idx: batch_idx + batch_size]
            batch = []
            batch_group_lengths = []
            batch_start_positions = []
            batch_length = 0
            for idx, length in batch_indices_and_lengths:
                batch_length += length

                group_length = 1
                batch_start_positions.append(0)
                batch.append(inputs[idx][:self.max_length])

                # split sequence into multiple sequences if it is longer than the specified max length
                if length > self.max_length:
                    start_pos = 0
                    while start_pos + self.max_length < length:
                        start_pos = min(start_pos + self.overlap, length - self.max_length)
                        batch_start_positions.append(start_pos)
                        batch.append(inputs[idx][start_pos:start_pos + self.max_length])
                        group_length += 1

                batch_group_lengths.append(group_length)

            pbar.set_description(
                f"[Batch {i + 1}] Repairing tokenization of {len(batch_indices_and_lengths):,} sequences "
                f"with {batch_length:,} characters in total"
            )

            kwargs = {
                "no_spaces": [" " not in ipt for ipt in batch]
            }
            # add inference keyword arguments to the model
            kwargs.update(self.inference_kwargs)
            batch_inference_results = self.model.inference(
                batch,
                **kwargs
            )
            for (idx, _), inference_results, start_positions in zip(
                    batch_indices_and_lengths,
                    split(batch_inference_results, batch_group_lengths),
                    split(batch_start_positions, batch_group_lengths)
            ):
                all_inference_results[idx] = self._merge_inference_results(inference_results, start_positions)

            pbar.update(batch_length)

        pbar.close()
        return all_inference_results

    def repair_text(
            self,
            inputs: StringInputOutput,
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = False
    ) -> StringInputOutput:
        input_is_string = isinstance(inputs, str)
        assert (
                input_is_string
                or (isinstance(inputs, list) and len(inputs) > 0 and isinstance(inputs[0], str))
        ), f"input needs to be a string or a non empty list of strings"

        if input_is_string:
            inputs = [inputs]

        # clean inputs from leading, trailing or multiple whitespaces
        inputs = [nlp.clean_sequence(ipt) for ipt in inputs]

        inference_results = self._repair_text_raw(inputs, batch_size, sort_by_length, show_progress)

        outputs = [
            tokenization_repair.repair_whitespace(
                ipt,
                ir.predictions[1:-1]
            )
            for ipt, ir in zip(inputs, inference_results)
        ]
        return outputs[0] if input_is_string else outputs

    def repair_text_iter(
            self,
            input_iterator: Iterable[StringInputOutput],
            batch_size: int = 16,
            sort_by_length: bool = True
    ) -> Iterable[str]:
        for inputs in input_iterator:
            yield self.repair_text(inputs, batch_size, sort_by_length)

    def repair_file(
            self,
            input_file_path: str,
            output_file_path: Optional[str] = None,
            batch_size: int = 16,
            sort_by_length: bool = True,
            show_progress: bool = True
    ) -> Optional[List[str]]:
        inputs = []
        with open(input_file_path, "r", encoding="utf8") as in_file:
            for line in in_file:
                inputs.append(line.strip())

        outputs = self.repair_text(inputs, batch_size, sort_by_length, show_progress)

        if output_file_path is not None:
            with open(output_file_path, "w", encoding="utf8") as out_file:
                for output in outputs:
                    out_file.write(output)
                    out_file.write("\n")
            return None
        else:
            return outputs

    def to(self, device: Union[str, int]) -> "TokenizationRepairer":
        self.device = torch.device(device)
        self.model = self.model.to(self.device)
        return self
