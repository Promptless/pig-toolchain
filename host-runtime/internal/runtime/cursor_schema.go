package runtime

// Cursor 3.19 field allowlist; unknown application fields are never exported.
type wireField struct{ Name, Kind string }

var cursorSchemas = map[string]map[int]wireField{
	"error": {
		1: {"message", "text"},
		2: {"reason", "text"},
	},
	"shell": {
		1:  {"command", "text"},
		2:  {"working_directory", "text"},
		3:  {"exit_code", "int"},
		4:  {"signal", "text"},
		5:  {"stdout", "text"},
		6:  {"stderr", "text"},
		7:  {"execution_time_ms", "int"},
		8:  {"output_location", "text"},
		10: {"interleaved_output", "text"},
		15: {"output_head", "text"},
		16: {"output_tail", "text"},
		17: {"elided_chars", "int"},
	},
	"shell_failure": {
		1:  {"command", "text"},
		2:  {"working_directory", "text"},
		3:  {"exit_code", "int"},
		5:  {"stdout", "text"},
		6:  {"stderr", "text"},
		9:  {"interleaved_output", "text"},
		11: {"aborted", "bool"},
		13: {"output_head", "text"},
		14: {"output_tail", "text"},
		15: {"elided_chars", "int"},
	},
	"read_legacy": {
		1: {"path", "text"},
		2: {"content", "text"},
		3: {"total_lines", "int"},
		4: {"file_size", "int"},
		6: {"truncated", "bool"},
		7: {"output_blob_id", "ref"},
	},
	"read": {
		1:  {"content", "text"},
		2:  {"is_empty", "bool"},
		3:  {"exceeded_limit", "bool"},
		4:  {"total_lines", "int"},
		5:  {"file_size", "int"},
		7:  {"path", "text"},
		9:  {"data_blob_id", "ref"},
		10: {"content_blob_id", "ref"},
	},
	"edit": {
		1: {"path", "text"},
		3: {"lines_added", "int"},
		4: {"lines_removed", "int"},
		5: {"diff", "text"},
		6: {"before", "text"},
		7: {"after", "text"},
		8: {"message", "text"},
	},
	"grep": {
		1: {"pattern", "text"},
		2: {"path", "text"},
		3: {"output_mode", "text"},
		4: {"workspaces", "[]grep_entry"},
		5: {"active_editor", "grep_union"},
	},
	"grep_entry": {
		1: {"workspace", "text"},
		2: {"result", "grep_union"},
	},
	"grep_union": {
		1: {"count", "grep_count"},
		2: {"files", "grep_files"},
		3: {"content", "grep_content"},
	},
	"grep_count": {
		1: {"counts", "[]grep_file_count"},
		2: {"total_files", "int"},
		3: {"total_matches", "int"},
	},
	"grep_file_count": {
		1: {"file", "text"},
		2: {"count", "int"},
	},
	"grep_files": {
		1: {"files", "[]text"},
		2: {"total_files", "int"},
	},
	"grep_content": {
		1: {"matches", "[]grep_file"},
		2: {"total_lines", "int"},
		3: {"total_matched_lines", "int"},
		4: {"client_truncated", "bool"},
		5: {"ripgrep_truncated", "bool"},
	},
	"grep_file": {
		1: {"file", "text"},
		2: {"matches", "[]grep_match"},
	},
	"grep_match": {
		1: {"line_number", "int"},
		2: {"content", "text"},
		3: {"content_truncated", "bool"},
		4: {"is_context_line", "bool"},
	},
	"mcp": {
		1: {"content", "[]mcp_content"},
		2: {"is_error", "bool"},
		3: {"structured_content", "struct"},
	},
	"mcp_content": {
		1: {"text", "mcp_text"},
		2: {"image", "omitted"},
	},
	"mcp_text": {
		1: {"text", "text"},
	},
	"web": {
		1: {"references", "[]web_reference"},
	},
	"web_reference": {
		1: {"title", "text"},
		2: {"url", "text"},
		3: {"chunk", "text"},
	},
	"task": {
		1: {"steps", "[]step"},
		2: {"agent_id", "text"},
		3: {"is_background", "bool"},
		4: {"duration_ms", "int"},
		5: {"result_suffix", "text"},
	},
	"assistant": {
		1: {"text", "text"},
		2: {"started_at_ms", "int"},
		3: {"completed_at_ms", "int"},
	},
	"thinking": {
		1: {"text", "text"},
		2: {"duration_ms", "int"},
	},
	"delete": {
		1: {"path", "text"},
		2: {"deleted_file", "text"},
		3: {"file_size", "int"},
		4: {"before", "text"},
	},
	"web_fetch": {
		1: {"url", "text"},
		2: {"markdown", "text"},
		3: {"output_location", "text"},
	},
}
