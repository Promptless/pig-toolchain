package runtime

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestCursorBubblePreservesTextUnlessEntireValueIsJSON(t *testing.T) {
	for _, tc := range []struct {
		input string
		want  any
	}{
		{"404 Not Found", "404 Not Found"},
		{"true story", "true story"},
		{`{"a":1} trailing`, `{"a":1} trailing`},
		{`{} []`, `{} []`},
		{"123", json.Number("123")},
		{"{\"a\":1} \n\t", object{"a": json.Number("1")}},
		{`"complete"`, "complete"},
		{"plain text", "plain text"},
	} {
		t.Run(tc.input, func(t *testing.T) {
			for _, key := range []string{"params", "rawArgs"} {
				records := bubbleEvents("bubble", object{"toolFormerData": object{"name": "shell", key: tc.input, "result": tc.input}}, savedResult{nil, "missing"}, object{})
				if len(records) != 2 {
					t.Fatalf("records: %v", records)
				}
				input := obj(records[0]["event"])["input"]
				output := obj(obj(records[1]["event"])["output"])["ui_result"]
				if !reflect.DeepEqual(input, tc.want) || !reflect.DeepEqual(output, tc.want) {
					t.Errorf("%s: input=%#v output=%#v want=%#v", key, input, output, tc.want)
				}
			}
		})
	}
}
