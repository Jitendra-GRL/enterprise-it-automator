"""Wraps this project's own pluggable LLM (app/agent/llm.py's provider
factory — Groq/Anthropic/watsonx/OpenRouter, whichever LLM_PROVIDER is
configured) as a DeepEval judge model, so GEval calls go through the SAME
free-tier keys already configured for the agent itself — zero additional
cost or credential surface, no OpenAI dependency (DeepEval's default judge
is GPT-4o-mini, which this project has no key for and no reason to add).

DeepEvalBaseLLM requires generate/a_generate/get_model_name/load_model
(confirmed against the installed deepeval package's actual abstract method
set, not assumed from docs — see evals/run_reasoning_quality.py's module
docstring for the verification trail). load_model is a no-op here: this
project's LLM objects (langchain_core BaseChatModel instances, or
FallbackLLM) are already fully constructed by get_llm()/FallbackLLM(),
there's nothing further to "load."
"""

from __future__ import annotations

from deepeval.models.base_model import DeepEvalBaseLLM


class ProjectLLMJudge(DeepEvalBaseLLM):
    """`llm` is anything with an async .ainvoke(messages) -> response
    method whose `.content` is the model's text reply — the same minimal
    interface every agent-graph node already relies on (a langchain_core
    BaseChatModel, or app.agent.llm.FallbackLLM). Synchronous .generate()
    is required by DeepEvalBaseLLM's abstract interface but this project
    is async-first throughout, so it's implemented via asyncio.run rather
    than a genuinely separate sync code path — DeepEval's own GEval.measure()
    (sync) vs .a_measure() (async) choose which one actually gets called;
    this codebase's own evals/run_*.py scripts always use the async path.
    """

    def __init__(self, llm, model_name: str) -> None:
        self._llm = llm
        self._model_name = model_name

    def load_model(self):
        return self

    async def a_generate(self, prompt: str, **kwargs) -> str:
        from langchain_core.messages import HumanMessage

        response = await self._llm.ainvoke([HumanMessage(content=prompt)])
        return str(response.content)

    def generate(self, prompt: str, **kwargs) -> str:
        import asyncio

        return asyncio.run(self.a_generate(prompt, **kwargs))

    def get_model_name(self, *args, **kwargs) -> str:
        return self._model_name
