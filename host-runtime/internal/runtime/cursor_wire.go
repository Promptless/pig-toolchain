package runtime

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"math"
	"strings"
	"unicode/utf8"
)

const cursorBlobLimit = 8 << 20

type wireValue struct {
	Bytes  []byte
	Int    uint64
	Scalar bool
}
type wireMessage map[int][]wireValue

func wireFields(data []byte) (wireMessage, error) {
	if len(data) > cursorBlobLimit {
		return nil, errors.New("cursor_blob_size_limit")
	}
	result := wireMessage{}
	count := 0
	for len(data) > 0 {
		count++
		if count > 100000 {
			return nil, errors.New("cursor_field_limit")
		}
		tag, n := binary.Uvarint(data)
		if n <= 0 {
			return nil, errors.New("cursor_invalid_varint")
		}
		data = data[n:]
		number, kind := int(tag>>3), tag&7
		if number == 0 {
			return nil, errors.New("cursor_invalid_field")
		}
		var value wireValue
		if kind == 0 {
			v, n := binary.Uvarint(data)
			if n <= 0 {
				return nil, errors.New("cursor_invalid_varint")
			}
			data = data[n:]
			value = wireValue{Int: v, Scalar: true}
		} else {
			var size uint64
			switch kind {
			case 2:
				v, n := binary.Uvarint(data)
				if n <= 0 {
					return nil, errors.New("cursor_invalid_varint")
				}
				data = data[n:]
				size = v
			case 1:
				size = 8
			case 5:
				size = 4
			default:
				return nil, errors.New("cursor_unsupported_wire_type")
			}
			if size > uint64(len(data)) {
				return nil, errors.New("cursor_incomplete_field")
			}
			value.Bytes = data[:size]
			data = data[size:]
		}
		result[number] = append(result[number], value)
	}
	return result, nil
}
func (m wireMessage) blob(number int) []byte {
	v := m[number]
	if len(v) == 0 || v[len(v)-1].Scalar {
		return nil
	}
	return v[len(v)-1].Bytes
}
func (m wireMessage) text(number int) (string, error) {
	b := m.blob(number)
	if !utf8.Valid(b) {
		return "", errors.New("cursor_invalid_utf8")
	}
	return string(b), nil
}
func wireStruct(data []byte, depth int) (object, error) {
	if depth > 12 {
		return nil, errors.New("cursor_depth_limit")
	}
	fields, err := wireFields(data)
	if err != nil {
		return nil, err
	}
	result := object{}
	for _, entry := range fields[1] {
		if entry.Scalar {
			continue
		}
		pair, err := wireFields(entry.Bytes)
		if err != nil {
			return nil, err
		}
		if _, ok := pair[1]; !ok {
			continue
		}
		key, err := pair.text(1)
		if err != nil {
			return nil, err
		}
		value, err := wireJSONValue(pair.blob(2), depth+1)
		if err != nil {
			return nil, err
		}
		result[key] = value
	}
	return result, nil
}
func wireJSONValue(data []byte, depth int) (any, error) {
	if depth > 12 {
		return nil, errors.New("cursor_depth_limit")
	}
	m, err := wireFields(data)
	if err != nil {
		return nil, err
	}
	if _, ok := m[2]; ok {
		b := m.blob(2)
		if len(b) != 8 {
			return nil, errors.New("cursor_invalid_number")
		}
		n := math.Float64frombits(binary.LittleEndian.Uint64(b))
		if math.IsInf(n, 0) || math.IsNaN(n) {
			return nil, errors.New("cursor_nonfinite_number")
		}
		return n, nil
	}
	if _, ok := m[3]; ok {
		return m.text(3)
	}
	if v, ok := m[4]; ok {
		return v[len(v)-1].Int != 0, nil
	}
	if _, ok := m[5]; ok {
		return wireStruct(m.blob(5), depth+1)
	}
	if _, ok := m[6]; ok {
		items, err := wireFields(m.blob(6))
		if err != nil {
			return nil, err
		}
		result := []any{}
		for _, item := range items[1] {
			if !item.Scalar {
				v, err := wireJSONValue(item.Bytes, depth+1)
				if err != nil {
					return nil, err
				}
				result = append(result, v)
			}
		}
		return result, nil
	}
	return nil, nil
}
func wireStep(data []byte, depth int) (object, error) {
	m, err := wireFields(data)
	if err != nil {
		return nil, err
	}
	for _, entry := range []struct {
		number int
		name   string
	}{{1, "assistant"}, {3, "thinking"}} {
		if _, ok := m[entry.number]; ok {
			v, err := decodeWire(m.blob(entry.number), entry.name, depth+1)
			return object{entry.name: v}, err
		}
	}
	if _, ok := m[2]; ok {
		id, result, quality, err := toolResult(m.blob(2), depth+1)
		return object{"call_id": nullable(id), "result": result, "completeness": quality}, err
	}
	return object{"completeness": "unsupported"}, nil
}
func decodeWire(data []byte, schema string, depth int) (object, error) {
	if depth > 12 {
		return nil, errors.New("cursor_depth_limit")
	}
	fields, err := wireFields(data)
	if err != nil {
		return nil, err
	}
	result := object{}
	for number, values := range fields {
		entry, ok := cursorSchemas[schema][number]
		if !ok {
			continue
		}
		repeated := strings.HasPrefix(entry.Kind, "[]")
		kind := strings.TrimPrefix(entry.Kind, "[]")
		decoded := []any{}
		for _, value := range values {
			var v any
			var err error
			if value.Scalar {
				if kind == "int" {
					v = value.Int
				} else if kind == "bool" {
					v = value.Int != 0
				} else {
					continue
				}
			} else {
				switch kind {
				case "text":
					if !utf8.Valid(value.Bytes) {
						return nil, errors.New("cursor_invalid_utf8")
					}
					v = string(value.Bytes)
				case "ref":
					v = hex.EncodeToString(value.Bytes)
				case "struct":
					v, err = wireStruct(value.Bytes, depth+1)
				case "step":
					v, err = wireStep(value.Bytes, depth+1)
				case "omitted":
					v = object{"completeness": "unsupported", "reason": "binary_content"}
				default:
					if _, ok := cursorSchemas[kind]; !ok {
						continue
					}
					v, err = decodeWire(value.Bytes, kind, depth+1)
				}
			}
			if err != nil {
				return nil, err
			}
			decoded = append(decoded, v)
		}
		if len(decoded) > 0 {
			if len(decoded) == 1 && !repeated {
				result[entry.Name] = decoded[0]
			} else {
				result[entry.Name] = decoded
			}
		}
	}
	return result, nil
}
func toolResult(data []byte, depth int) (string, object, string, error) {
	if depth > 12 {
		return "", nil, "", errors.New("cursor_depth_limit")
	}
	m, err := wireFields(data)
	if err != nil {
		return "", nil, "", err
	}
	id, err := m.text(57)
	if err != nil {
		return "", nil, "", err
	}
	kinds := []struct {
		number int
		schema string
	}{{1, "shell"}, {3, "delete"}, {5, "grep"}, {8, "read"}, {12, "edit"}, {15, "mcp"}, {18, "web"}, {19, "task"}, {37, "web_fetch"}}
	for _, entry := range kinds {
		if _, ok := m[entry.number]; !ok {
			continue
		}
		call, err := wireFields(m.blob(entry.number))
		if err != nil {
			return id, nil, "", err
		}
		if len(call.blob(2)) == 0 {
			return id, object{}, "missing", nil
		}
		variants, err := wireFields(call.blob(2))
		if err != nil {
			return id, nil, "", err
		}
		if _, ok := variants[1]; ok {
			schema := entry.schema
			success, err := wireFields(variants.blob(1))
			if err != nil {
				return id, nil, "", err
			}
			if values := success[2]; schema == "read" && len(values) > 0 && !values[len(values)-1].Scalar {
				schema = "read_legacy"
			}
			result, err := decodeWire(variants.blob(1), schema, depth)
			if err != nil {
				return id, nil, "", err
			}
			result["status"] = "success"
			quality := "available"
			if schema == "task" {
				quality = "partial"
			}
			if schema == "mcp" {
				content, _ := result["content"].([]any)
				for _, item := range content {
					if _, ok := obj(item)["image"]; ok {
						quality = "partial"
					}
				}
			}
			return id, result, quality, nil
		}
		if _, ok := variants[2]; ok {
			schema := "error"
			if entry.schema == "shell" {
				schema = "shell_failure"
			}
			result, err := decodeWire(variants.blob(2), schema, 0)
			return id, object{"error": result}, "available", err
		}
		return id, object{}, "unsupported", nil
	}
	return id, object{}, "unsupported", nil
}
