# Notebook UI And LangChain Fallback Steps

- [x] Inspect the current runtime and identify the narrow integration points for notebook UI and tool-calling fallback.
- [x] Add a Jupyter notebook UI module that reuses the existing engine instead of duplicating the runtime.
- [x] Add a LangChain-based fallback path for OpenAI-compatible providers when native tool-calling requests are rejected.
- [x] Add focused tests for the notebook UI and fallback behavior.
- [x] Update docs and package metadata for the new optional integrations.
