from __future__ import annotations

import os

import httpx

from .rag_models import DeepSeekConfig, RagAnswer, RetrievalResult


class DeepSeekGenerator:
    def __init__(self, config: DeepSeekConfig | None = None) -> None:
        self.config = config or DeepSeekConfig()

    def generate(self, *, query_text: str, retrieval: RetrievalResult) -> RagAnswer:
        api_key = os.getenv(self.config.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"DeepSeek generation requires {self.config.api_key_env} in the environment."
            )

        context_text = "\n\n".join(
            [
                f"## Context Block {index + 1}\n{block.rendered_text}"
                for index, block in enumerate(retrieval.context_blocks)
            ]
        )
        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [
                {"role": "system", "content": self.config.system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{query_text}\n\n"
                        "Use only the retrieved QQ chat evidence below. If the evidence is "
                        "insufficient, say so explicitly.\n\n"
                        f"{context_text}"
                    ),
                },
            ],
        }

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        answer_text = body["choices"][0]["message"]["content"]
        return RagAnswer(
            query_text=query_text,
            retrieval=retrieval,
            model=self.config.model,
            answer_text=answer_text,
            raw_response=body,
        )
