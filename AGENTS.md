# AGENTS.md

Minimal Feishu (Lark) bot with **pluggable workflows**: messages arrive over the official SDK's WebSocket long connection, a selected workflow (LangGraph + deepagents under the hood) processes them, and the adapter layer in `main.py` delivers the result (plain reply or a Feishu doc report link).

- `main.py` — Feishu adapter: drops non-text/non-user messages, dedupes by message id, runs work on a single-worker `ThreadPoolExecutor`, delivers `WorkflowResult` (reply text / create report doc).
- `workflows/` — registry pattern (`workflows/__init__.py`): each workflow lives in its OWN package directory named after it, whose `__init__.py` exports `build(ctx) -> Callable[[str, MessageContext | None], WorkflowResult]` decorated with `@register("key")`; selected via env `QR_WORKFLOW` (default `simple`). Add new workflows by creating a package, registering, and importing it at the bottom of `workflows/__init__.py`.
  - `base.py` — `WorkflowResult`, `MessageContext` (chat_id/chat_type + `on_accepted` ack callback from the adapter; API callers pass None), `WorkflowContext` (dependency injection: model, lark client, workspace root, projects file, doc_meta/doc_content fetchers), `final_answer`.
  - `simple_agent/` — SimpleAgentWorkflow (chat assistant, tools rooted at home or `QR_SIMPLE_ROOT`). Deliberately a single-file package — don't split it.
  - `dev_review/` — DevProcessReviewWorkflow, split by concern: `prompts.py` (ALL copy: intent/review prompts, refuse texts, REVIEW_TYPES — wording changes go here only), `state.py` (`Intent` schema + `ReviewState`), `__init__.py` (LangGraph pipeline `classify_intent → resolve_project → init_workspace → {clone_code ∥ clone_knowbase ∥ fetch_doc} → review (deepagents) → write_result` + `refuse` node, `load_projects`, `_sync_repo`). Delivery stays OUTSIDE the graph (adapter in `main.py`) by design — the graph must stay channel-agnostic for the future FastAPI entry. The workspace is PERSISTENT per project (`workspaces/<project>/`): `_sync_repo` clones once then pulls, requirement docs are cached as `docs/<title>-<revision>.md` (same revision reuses the local file), results go to timestamped `result/review-<ts>.md` files. The review prompt names the exact doc file + label (`《title》rN`) because `docs/` holds historical versions.
- `feishu_docs.py` — Feishu doc APIs: `doc_meta` (wiki link → `space.get_node`, then `document.get` for title + `revision_id`), `fetch_doc_content` (docx `raw_content` by document_id), and report doc creation (`document.create` + `document_block_children.create`, markdown subset → docx blocks). After creation `_open_link_share` patches `link_share_entity=tenant_readable` so org members can open the link (app-owned docs are private by default); failure is logged, never fatal. `Document` responses carry no `url` field — links are built as `{QR_FEISHU_DOMAIN}/docx/{document_id}`.
- `projects.json` — project registry (name/aliases/chat_ids → code_repo / know_base_repo); path overridable via `QR_PROJECTS_FILE`. Project resolution in `resolve_project` is deterministic and layered — text mention > `chat_ids` binding > single-project default — deliberately NO LLM call; the missing-link check runs AFTER it so `refuse` attributes the right reason.
- `workspaces/` — per-run workspace dirs for dev_review (gitignored, never commit).

## Commands

```bash
uv sync                          # install deps from uv.lock into .venv
uv run main.py                   # run the bot (.env auto-loaded via load_dotenv in main.py)
uv run pytest                    # test suite
```

Requires Python ≥ 3.12 and uv. There is no linter or formatter configured — don't hunt for one.

Runtime config lives in `.env` (gitignored, contains secrets — never print or commit it): `QR_FEISHU_APP_ID`, `QR_FEISHU_APP_SECRET`, `ZHIPU_API_KEY`, `ZHIPU_BASE_URL`, `QR_MODEL_NAME`, `QR_WORKFLOW` (`simple` | `dev_review`), plus optional `QR_WORKSPACE_ROOT`, `QR_PROJECTS_FILE`, `QR_REPORT_FOLDER_TOKEN`, `QR_FEISHU_DOMAIN`, `QR_SIMPLE_ROOT`. Feishu app setup (long-connection mode, event subscription, permissions) is documented in README.md.

## Conventions

- Docstrings, comments, log messages, and agent system prompts are in Chinese; write new ones in Chinese to match.
- Module-level `LOG = logging.getLogger("quality-reviewer-lite")`; INFO for ignored/duplicate messages, `LOG.exception` in catch-alls.
- Tests use fakes end-to-end: `FakeModel.with_structured_output` for intent, monkeypatched `create_deep_agent` in the workflow modules, injected `doc_fetcher`. Follow this pattern instead of mocking HTTP.

## Gotchas

- **Legacy naming**: the pyproject package name, logger name, and env-var prefix (`QR_`) come from an earlier project ("quality-reviewer-lite"); the repo is `mini-feishu-bot`. Keep the `QR_` prefix — deployed `.env` files depend on it.
- `zhipu_base_url()` in `main.py` normalizes the base URL: any value not ending in `/v4` gets `/paas/v4` appended.
- `ThreadPoolExecutor(max_workers=1)` is deliberate: runs are serialized; the pool only exists so the SDK event loop stays unblocked.
- `_bot_open_id()` uses a generic `BaseRequest` because lark-oapi has no typed wrapper for `bot/v3/info`.
- In the dev_review graph, the three prep branches run in one langgraph superstep; any state key they both write must have a reducer (`prep_notes` uses `operator.add`) or langgraph raises `InvalidUpdateError`.
- `refuse` keeps an already-set `reply_text` (fetch failures write their reason there) — don't regenerate the text unconditionally.
- Acceptance ack: `MessageContext.on_accepted` (fired in `init_workspace` after intent/project/link pass) is implemented by the adapter as a `Typing` (敲键盘) emoji reaction (`im:message` scope already covers it; falls back to a text reply). Rejected/unsupported messages never fire it. The adapter keeps `message_id -> reaction_id` in `self._acks` and deletes the reaction after the final reply, so the emoji only exists while processing.
- `ChatOpenAI` runs with `timeout=300, max_retries=1` — deliberately no long retry chains: a stalled zhipu request would otherwise blind-wait minutes per retry. The review prompt forbids reading lock files/bulk artifacts (a 340KB `uv.lock` in context stalls prefill).
- The report markdown passed to `feishu_docs.create_report_doc` must stay within the converter's subset (`#`/`##`/`###`, `-`/`1.` lists, fenced code, `**bold**`, `` `inline code` ``); anything else degrades to plain paragraphs.
- `with_structured_output` on the zhipu endpoint MUST pass `method="function_calling"`: langchain-openai 1.x defaults to `method="json_schema"`, and GLM wraps the JSON in ```json fences which fails Pydantic parsing (verified live, 2026-09).
- lark-oapi request/response models serialize via `filter_null` — unset `None` fields are stripped, so setting only needed fields on a `Block`/`Text` object is safe.

## Security

Agents execute arbitrary shell commands with `inherit_env=True` — any Feishu user who can message the bot can run commands on the host. `dev_review` scopes the deep agent's `LocalShellBackend` to the per-run workspace; `simple` defaults to the home dir (`QR_SIMPLE_ROOT` narrows it). Before changing agent backends, tools, or roots, read the ⚠️ 安全须知 section in README.md.
