# MCP distribution

Instruction Hub can compile its authored assets into an offline MCP bundle as
well as plugin marketplaces. A separate read-only server delivers that bundle to
clients. Client behavior, including Dots support for skill activation, remains
unverified until tested in that client.

## Build and serve

Enable the additional distribution channel in `hub.yaml`:

```yaml
mcp:
  enabled: true
```

Keep the existing `targets` list: MCP is a distribution channel, not an executing
harness. Existing target validation still applies. The default is disabled.

```bash
pig verify --hub .       # Compile in a temporary directory; leave the hub untouched.
pig build --hub .        # Write marketplaces and dist/mcp.
pig build --hub . --check
```

Compilation needs only the base toolchain dependencies. Install the optional
server dependencies from the toolchain checkout:

```bash
uv sync --extra mcp
uv run pig serve-mcp --bundle /path/to/hub/dist/mcp --plugin dev
```

For an installed toolchain, install it with the `mcp` extra, for example
`uv pip install '/path/to/instruction-hub-toolchain[mcp]'`, then use
`pig serve-mcp`. The server defaults to stdio and keeps protocol output on stdout.
Repeat `--plugin` to select several plugins; omit it to expose all stable plugins.
Unknown plugin IDs fail startup. Every protocol surface uses the same selection,
including direct reads of guessed URIs. A shared asset appears once in the asset
catalog and records only its selected plugin memberships. Skill-loading tools
have one alias per selected plugin membership.

For Streamable HTTP:

```bash
# Provision INSTRUCTION_HUB_MCP_TOKEN in the process environment.
pig serve-mcp --bundle /path/to/hub/dist/mcp \
  --plugin dev --transport streamable-http \
  --host 0.0.0.0 --port 8000 --allowed-host hub.example.com
```

Connect to `/mcp` with `Authorization: Bearer <token>`. Public deployments need
TLS termination and an allowed Host matching the externally visible hostname.
`--token-env` changes which environment variable supplies the token. Non-loopback
binds require it; loopback defaults permit unauthenticated local development.
When forwarding a loopback listener through a proxy, set a token there too.
Requests carrying an Origin header are rejected; this configuration targets
server-side MCP clients, not browser JavaScript.

The credential grants access to the instance's entire plugin selection. There
is no OAuth flow, delegated identity, per-user authorization, or tenant routing.
Run separate instances and credentials for different access scopes. Dots must
support supplying this credential, or a later authentication adapter is needed.

## Asset representations

| Asset | Delivered representation | Behavior the client must supply |
| --- | --- | --- |
| Skill | Plugin-prefixed loading tool, skills extension entry, and all supporting files | Load instructions, materialize relative files when needed, provide dependencies and authorized execution. |
| Command | MCP prompt plus original source | Explicit user invocation and execution. Host macros or argument syntax outside the portable subset remain resources-only. |
| Agent | Plugin-prefixed loading tool, delegation skill, and original source | Spawn a child agent. Tool restrictions are advisory; model overrides are omitted. The generated skill stops if delegation is unavailable. |
| Rule | Original resource, including authored scope metadata | Scope matching, precedence, and activation. Delivery does not provide always-on enforcement. |
| Hook | Source files and structured hook declaration | Host lifecycle events, registration, and execution. The server cannot fire hooks. |
| MCP configuration | Original connection definition | Connect to upstream servers and supply their credentials. The distributor does not proxy their tools or interpolate secrets. |

The bundle reports each asset's representation, requirements, projection
limitations, original target support, source hash, and plugin memberships. These
are separate from verified client support: `client_verification` starts as
`unverified`. Native support on a marketplace target does not prove MCP fidelity.

Skills retain their authored bytes and JSON-compatible YAML frontmatter,
including unknown fields. Root `skill.md` casing is normalized to `SKILL.md`.
Ambiguous or non-JSON frontmatter is not advertised through the skills extension;
its original source remains readable with a reason. The same resources-only
fallback applies when an agent or command cannot be projected faithfully.

`asset.yaml` at an asset root is compiler metadata; the bundle records the
relevant declarations rather than distributing that file. Nested supporting
files named `asset.yaml` are retained. File modes are not distributed: clients
should invoke scripts through the appropriate interpreter. Paths inside skill
instructions are preserved; plugin-root variables, machine paths, references to
other assets, and external dependencies are not automatically resolved.

External plugins retain pinned source metadata and an explicit limitation. Their
payloads are not fetched or exported by this compiler. Toolchain-managed plugin
update/install skills and trace-ingestion runtimes are not included. The `pig`
plugin may therefore expose no authored assets. Resource reads establish delivery,
not instruction compliance or enrollment in trace capture.

## Protocol contract

The server advertises `io.modelcontextprotocol/skills` and implements
`skills/list`, `skills/get`, and `resources/read` according to the
[MCP skills extension](https://github.com/modelcontextprotocol/ext-skills).
Each skill lists every file with its byte size and SHA-256 digest. Skill URIs
include the marketplace, bundle identity, asset kind and ID, and frontmatter name;
the final directory matches that name. This disambiguates equal skill names in
different assets without rewriting their frontmatter.

`resources/list` exposes all selected files and a selected `catalog.json`.
`prompts/list` and `prompts/get` expose portable commands without arguments.
Every projected skill is also an ordinary read-only MCP tool. A skill with asset
ID `review-change` in plugin `dev` is named `dev__load_skill_review-change`.
Delegation skill tools use `dev__load_agent_<asset-id>`. Names use stable plugin
and asset IDs; editing frontmatter does not rename a tool. A skill shared by
several plugins has an alias for each selected plugin.

Names are limited to 64 characters for tool-client compatibility. Long asset IDs
are shortened with an eight-character SHA-256 suffix; the complete plugin prefix
is preserved. Shortening requires room for an asset-ID character, separator, and
digest; an overlong plugin prefix fails compilation and catalog loading with a
request to shorten the plugin ID. Duplicate generated names also fail.

Tool descriptions preserve the authored skill trigger and identify the operation
as loading instructions. These tools accept no arguments and return the complete
`SKILL.md`, compatibility information, plugin and bundle identity, and supporting
files with relative paths and exact `read_asset` arguments. This operation does
not run scripts or carry out the workflow. The client follows the instructions
with its available capabilities and authorization. Supporting-file calls include
`bundle_id` to prevent reads from a different release after a server replacement.

Two additional read-only tools provide general catalog access:

- `search_assets`: discover by words, optional asset kind, and cursor; an empty
  query lists assets. Returns metadata, compatibility requirements, and paths.
- `read_asset`: retrieve a discovered asset by `ref` and optional exact file
  path. Pass the returned `bundle_id` to bind the read to that release; a different
  bundle ID is rejected. The default reads the skill entrypoint or first source
  file. Binary content uses base64; text uses UTF-8. No source code is executed.

List methods paginate. Cursors are bound to the bundle, selected plugins, and
query. Skills list/get return `resultType: complete`, `ttlMs: 0`, and
`cacheScope: private`. The service needs neither filesystem access in the client
nor client capabilities to deliver bytes; useful activation may need both.

The standard skill bound is 512 files and 16 MiB per skill. Oversized skills stay
resources-only. A bundle exceeding 10,000 files or 256 MiB, or with a manifest
larger than 16 MiB, fails compilation and startup. Particular importers can
impose tighter limits. In particular, the
[OpenAI plugin importer](https://developers.openai.com/plugins/build/mcp-server#import-skills-from-the-mcp-server)
documents a bounded import profile; this implementation does not claim that an
entire hub fits that profile or that Dots implements it.

## Releases and hosting

`dist/mcp/bundle.json` identifies the bundle from its canonical manifest,
including hashes of every file. MCP-enabled releases use release schema 4 and
include a directory hash in both `mcp_bundle` and `version_basis.mcp_bundle`.
MCP output changes participate in automatic release versioning. With MCP
disabled, the existing schema 2/3 release behavior is preserved. Publication
includes the bundle under the already-published `dist` tree; it does not deploy
or restart an MCP service.

At startup the server validates the manifest identity, paths, complete skill
manifests, file sizes, and digests, then loads an in-memory snapshot. Symlinks and
traversal paths are rejected. Unlisted files are never served. Source repository
access and network fetches are unnecessary at serving time.

Hashes check internal integrity, not publisher authenticity: deploy artifacts
from a trusted build and protect the artifact store and transport. Plugin views
are a serving boundary, not an encryption boundary; the deployment holding the
full bundle can access its other plugins too.

An instance serves one immutable bundle for its lifetime. There is no hot reload,
multi-version store, notification on release, or retention policy. Start a new
instance for an upgrade or rollback. If clients must finish using old URIs, retain
the old instance at its release-specific endpoint until those clients finish.
Replacing a stable endpoint alone makes old bundle URIs unavailable there.

## Dots acceptance before rollout

SDK tests exercise modern skills discovery and legacy tool retrieval, including
stdio and authenticated HTTP. They do not establish Dots compatibility. Use a
small plugin selection and a deployed test endpoint to verify:

1. Connection and authentication, discovery of plugin-prefixed skill tools across
   every `tools/list` page, and whether Dots
   also exposes resources, prompts, and the skills extension.
2. A natural task matching a skill description selects the corresponding loading
   tool without a prompt to search the hub. Confirm retrieval of the complete
   skill and a supporting file from the same bundle ID, then workflow execution.
3. Relative-file materialization and an explicitly authorized harmless script,
   if Dots provides execution. Missing dependencies must surface as limitations.
4. Explicit command invocation and child-agent delegation, if supported. Verify
   that unavailable capabilities produce an explanation instead of invented
   execution. Check rule activation separately from retrieval.
5. Denial of unselected assets through every exposed retrieval path, followed
   by an upgrade and rollback without mixing files from different releases.

OAuth, external-plugin export, host event adapters, an upstream MCP gateway,
dependency provisioning, and multi-version hosting are separate follow-on work.
