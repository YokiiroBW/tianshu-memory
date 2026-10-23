"""Offline CPU encoder for the explicitly configured optional Python runtime."""

import json
import os
import sys


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    from transformers import AutoModel, AutoTokenizer

    directory, revision, threads = sys.argv[1:]
    torch.set_num_threads(int(threads))
    tokenizer = AutoTokenizer.from_pretrained(
        directory, local_files_only=True, trust_remote_code=False
    )
    model = (
        AutoModel.from_pretrained(
            directory, local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        .to("cpu")
        .eval()
    )
    maximum = min(int(model.config.max_position_embeddings), 512) - 2
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request["revision"] != revision or len(request["texts"]) > 32:
                raise ValueError("Invalid embedding request")
            result = []
            for text in request["texts"]:
                if not isinstance(text, str) or not text or len(text.encode()) > 131072:
                    raise ValueError("Invalid embedding text")
                if request.get("kind") == "query":
                    text = "为这个句子生成表示以用于检索相关文章：" + text
                tokens = tokenizer.encode(text, add_special_tokens=False)
                parts = []
                for offset in range(0, len(tokens), maximum):
                    item = tokenizer.prepare_for_model(
                        tokens[offset : offset + maximum],
                        add_special_tokens=True,
                        return_tensors="pt",
                    )
                    item = {
                        key: value.unsqueeze(0) if value.ndim == 1 else value
                        for key, value in item.items()
                    }
                    with torch.inference_mode():
                        parts.append(model(**item).last_hidden_state[:, 0].float())
                vector = torch.nn.functional.normalize(torch.stack(parts).mean(0), p=2, dim=1)
                result.append(vector[0].tolist())
            answer = {"vectors": result}
        except Exception:
            # Source text, paths and third-party diagnostics never enter replies.
            answer = {"error": "embedding_failed"}
        print(json.dumps(answer, allow_nan=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
