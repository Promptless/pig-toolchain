# Token-efficient mode

PIG supports two independent, opt-in settings. Both default to `false`:

```yaml
token_efficiency:
  concise_output: true
  compact_instructions: false
```

`concise_output` installs a Claude hook in the canonical `pig` plugin. At
`SessionStart` and `SubagentStart`, it asks for concise prose while preserving
requirements, necessary reasoning, uncertainty, exact literals, and requested
detail. It explicitly forbids skipping work or checks to save tokens. It does
not change model selection, reasoning effort, tool permissions, or task scope.

`compact_instructions` is experimental input rewriting. It uses the same
deterministic, local compactor in two places:

- At build time, generated skill entrypoints in authored plugins, including
  converted agent/command skills, across the configured target hosts. MCP
  distribution also compacts skill entrypoints, agent projections, and command
  prompt bodies, with hashes and sizes calculated from the distributed bytes.
- In Claude, a `PreToolUse` hook for `Agent` and legacy `Task` rewrites only the
  `prompt` argument. It retains all other arguments and leaves permission
  decisions to the host.

Authored files are never rewritten. References, scripts, routing frontmatter,
and external plugins retain their authored bytes. Native rules, agents, and
commands outside generated skill entrypoints keep their normal rendering.
There is no proxy, runtime model call, or tool-output compression. Typed user
prompts cannot be replaced by `UserPromptSubmit`; that hook can add context,
which would increase input tokens. Codex, Cursor, Gemini, and MCP get the
generated content option but no token-efficiency runtime hook in this version.

## Install, inspect, and disable

Set the flags in `hub.yaml`, then run `pig verify --hub .` and publish through
the hub's normal PR/release workflow. Update the installed `pig` plugin and any
plugins whose generated skills changed, then start a fresh session. Managed
files belong to the toolchain; do not add hook files to the hub's source assets.

The Claude hook requires `python3` (3.9+) on the application's PATH and a host
version supporting `PreToolUse.updatedInput` for input rewriting. Local command
execution was tested on macOS and Linux; Windows/native desktop installation
has not been tested. It emits no context for unchanged or unknown inputs.
Malformed, oversized (over 1 MiB), or excessively nested JSON is skipped with a
generic stderr message that does not include prompt content.

Preview the exact input transformation without writing files:

```bash
pig compact-preview assets/skills/example/SKILL.md
```

The preview reports UTF-8 bytes, **not tokens**. To measure the actual corpus
with an explicitly named tokenizer, from a toolchain checkout run:

```bash
uv run --with tiktoken python scripts/measure_token_efficiency.py /path/to/hub --diff
```

Set either flag to `false` and rebuild/update to remove its behavior. Generated
skills revert to the source text when `compact_instructions` is disabled.
`PIG_TOKEN_EFFICIENT_MODE=off` in the Claude process environment disables future
runtime hook output. It cannot restore generated files or remove context
already injected into a running conversation; restart the session after
updating the plugin or environment.

## Why this design

[Caveman's hook configuration](https://github.com/JuliusBrussee/caveman/blob/d262ab3882952a59312ff3537cd86d894523c5ab/.claude-plugin/plugin.json)
uses session/subagent activation to inject concise-writing guidance. Its
separate prompt tracking hook does not delete words from the user's message.
PIG follows the context-injection approach for output, using a short instruction
instead of loading an entire style skill on every activation.

The [CAVEWOMAN study](https://arxiv.org/abs/2606.24083) found that its evaluated
input word-deletion methods could hurt accuracy and increase response length;
output constraints were more promising on its tested tasks. This is evidence
against treating grammatical words as universally disposable, not a guarantee
that any concise style is harmless. [LLMLingua-2](https://arxiv.org/abs/2403.12968)
uses a trained context-aware compressor and task evaluations. Its results do
not establish that generic stopword removal preserves instruction semantics.

[Anthropic's context engineering guidance](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
favors high-signal context and progressive retrieval. Keep skill routing
descriptions precise and move optional detail into referenced resources before
trying aggressive rewriting. Stable, deterministic build output also preserves
prefix reuse better than per-request paraphrasing; the importance of exact
prefixes follows from the [prompt caching contract](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

The compactor therefore has only seven reviewed phrase substitutions:

| Original | Replacement |
| --- | --- |
| in order to | to |
| due to the fact that | because |
| at this point in time | now |
| in the event that | if |
| for the purpose of | for |
| with the exception of | except |
| on a daily basis | daily |

It retains whitespace, frontmatter, fenced/indented code, and headings, and
skips paragraphs containing possible literals such as quotes, inline code,
links, paths, tables, HTML, or JSON. Multiline quotes cause the entire input to
be retained. This intentionally leaves many compression opportunities unused.
Even these substitutions are not provably lossless; exact wording can be part
of a task without being marked as a literal. Review diffs and keep input
rewriting disabled unless a representative evaluation supports it.

Hook event and replacement behavior follows the
[Claude hooks reference](https://code.claude.com/docs/en/hooks). `updatedInput`
replaces the input object, so the hook copies every unchanged argument and
does not emit a permission override.

## Initial evidence (2026-10-16)

The actual Promptless hub at `fdfecb7842e423176d01956885f1d7c56ffd4ce0` has 69
authored skill entrypoints. With `o200k_base`, the compactor produced **116,660
tokens before and after: zero savings**. None contained the seven phrases.
This counts each source entrypoint once, not an entire session or every plugin
duplicate. It excludes references, dynamically loaded context, and provider
billing. The concise instruction itself costs 65 tokens under this tokenizer
per activation; Claude's tokenizer and host wrapping differ.

A tool-disabled Claude evaluation used the 12 synthetic cases in
[`cases.json`](../tests/fixtures/token-efficiency/cases.json): approvals,
negation, exceptions, conditions, quantities, units, literals, code,
prerequisites, scope, uncertainty, and cause. Each of four arms answered the
same 12 independent cases in one request, with one sample per arm on each of
macOS and Linux. Both runs passed every case. The table shows the Linux rerun
with the committed evaluator; the reported model was `claude-opus-5-5`.

| Arm | Exact matches | Input tokens including cache creation/read | Output tokens |
| --- | --- | --- | --- |
| Baseline | 12/12 | 1,214 | 136 |
| Input compaction | 12/12 | 1,187 | 136 |
| Concise output | 12/12 | 1,329 | 136 |
| Both | 12/12 | 1,302 | 136 |

The fixed answer format leaves no response prose to shorten, so the concise
instruction adds overhead here. These results support preserving the tested
constraints; they do **not** demonstrate whole-session savings or unchanged
coding performance. They also do not test a desktop host loading the plugin.

A separate Linux Claude CLI smoke test loaded the generated plugin from a path
containing spaces. In a fresh session the model received the exact
`SessionStart` instruction; with `PIG_TOKEN_EFFICIENT_MODE=off`, it received no
such instruction. `SubagentStart` and `PreToolUse` are covered by contract and
subprocess tests, not a native end-to-end delegation test.

To repeat the paid smoke test with authenticated Claude CLI, from a toolchain
checkout run:

```bash
uv run python scripts/evaluate_token_efficiency.py --model claude-opus-5-5
```

The runner disables tools, installed plugins/hooks through safe mode, slash commands,
settings, and MCP; uses a temporary working directory; and sends only synthetic
fixtures. The compactor and concise instruction are supplied directly to
isolate their effects. It caps each of four requests at $2, disables session
persistence, and prints scores plus provider usage without session identifiers.
Model availability, tokenization, and sampling can change the result.

Before enabling input rewriting more broadly, compare representative coding
and skill-use tasks on supported models with repeated trials. Gate on task
completion, authorization/negation/literal preservation, tool validity, and
required verification, and measure total input plus output tokens, cache usage,
and latency. A smaller prompt alone is not an acceptance criterion.
