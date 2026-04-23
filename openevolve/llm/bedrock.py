"""
AWS Bedrock LLM interface.

Uses the Bedrock Runtime Converse API, which provides a uniform message format
across foundation models (Anthropic, Meta, Amazon, Mistral, Cohere, ...).

Authentication uses the standard boto3 credential chain (env vars,
~/.aws/credentials, instance/role profiles). No api_key is read from config.

Model IDs are Bedrock model IDs, e.g.:
  - "anthropic.claude-3-5-sonnet-20241022-v2:0"
  - "meta.llama3-1-70b-instruct-v1:0"
  - "amazon.nova-pro-v1:0"
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from openevolve.llm.base import LLMInterface

logger = logging.getLogger(__name__)


class BedrockLLM(LLMInterface):
    """LLM interface using AWS Bedrock's Converse API."""

    def __init__(self, model_cfg) -> None:
        try:
            import boto3
        except ImportError as e:
            raise ImportError(
                "boto3 is required for the Bedrock provider. "
                "Install with: pip install -e '.[bedrock]'"
            ) from e

        self.model = model_cfg.name
        self.system_message = model_cfg.system_message
        self.temperature = model_cfg.temperature
        self.top_p = model_cfg.top_p
        self.max_tokens = model_cfg.max_tokens
        self.timeout = model_cfg.timeout
        self.retries = model_cfg.retries
        self.retry_delay = model_cfg.retry_delay
        self.random_seed = getattr(model_cfg, "random_seed", None)

        region = (
            getattr(model_cfg, "region", None)
            or os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
        )
        if not region:
            raise ValueError(
                "Bedrock provider requires a region. Set `region` in the model config "
                "or the AWS_REGION / AWS_DEFAULT_REGION environment variable."
            )

        self.client = boto3.client("bedrock-runtime", region_name=region)

        if not hasattr(logger, "_initialized_bedrock_models"):
            logger._initialized_bedrock_models = set()
        if self.model not in logger._initialized_bedrock_models:
            logger.info(f"Initialized Bedrock LLM: {self.model} (region={region})")
            logger._initialized_bedrock_models.add(self.model)

    async def generate(self, prompt: str, **kwargs) -> str:
        return await self.generate_with_context(
            system_message=self.system_message,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )

    async def generate_with_context(
        self, system_message: str, messages: List[Dict[str, str]], **kwargs
    ) -> str:
        converse_messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]} for m in messages
        ]

        inference_config: Dict[str, Any] = {}
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        if max_tokens is not None:
            inference_config["maxTokens"] = int(max_tokens)
        temperature = kwargs.get("temperature", self.temperature)
        if temperature is not None:
            inference_config["temperature"] = float(temperature)
        top_p = kwargs.get("top_p", self.top_p)
        if top_p is not None:
            inference_config["topP"] = float(top_p)

        request: Dict[str, Any] = {
            "modelId": self.model,
            "messages": converse_messages,
            "inferenceConfig": inference_config,
        }
        if system_message:
            request["system"] = [{"text": system_message}]

        retries = kwargs.get("retries", self.retries) or 0
        retry_delay = kwargs.get("retry_delay", self.retry_delay) or 0
        timeout = kwargs.get("timeout", self.timeout)

        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(self._converse(request), timeout=timeout)
            except asyncio.TimeoutError:
                if attempt < retries:
                    logger.warning(
                        f"Bedrock timeout on attempt {attempt + 1}/{retries + 1}. Retrying..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"Bedrock: all {retries + 1} attempts timed out")
                    raise
            except Exception as e:
                if attempt < retries:
                    logger.warning(
                        f"Bedrock error on attempt {attempt + 1}/{retries + 1}: {e}. Retrying..."
                    )
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error(f"Bedrock: all {retries + 1} attempts failed: {e}")
                    raise

    async def _converse(self, request: Dict[str, Any]) -> str:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: self.client.converse(**request))
        logger.debug(f"Bedrock usage: {response.get('usage')}")
        return response["output"]["message"]["content"][0]["text"]
