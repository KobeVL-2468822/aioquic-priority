import json
import sys

# NOTE: only works for qlog files with traces originating from 1 vantage point
def convert_json_to_jsonseq(input_file, output_file):
    with open(input_file, "r", encoding="utf-8") as infile:
        data = json.load(infile)

    if "qlog_format" not in data or data["qlog_format"] != "JSON":
        raise ValueError("Invalid JSON v0.3 file format")

    # Default vantage point if not specified in input
    DEFAULT_VANTAGE_POINT = {"name": "client-1", "type": "client"}

    trace = data.get("traces", [])[0]
    common_fields = trace.get("common_fields", {})
    vantage_point = trace.get("vantage_point", DEFAULT_VANTAGE_POINT)

    # Construct header conform qlog JSON-SEQ format
    header = {
        "qlog_version": "0.3",
        "qlog_format": "JSON-SEQ",
        "file_schema": "urn:ietf:params:qlog:file:sequential",
        "serialization_format": "application/qlog+json-seq",
        "title": "Converted log",
        "description": "Converted from JSON v0.3",
        "trace": {
            "common_fields": {
                "ODCID": common_fields.get("ODCID", "unknown"),
                "time_format": "relative_to_epoch",
                "reference_time": {
                    "clock_type": "system",
                    "epoch": "1970-01-01T00:00:00.000Z"
                }
            },
            "vantage_point": vantage_point,
            "event_schemas": [
                "urn:ietf:params:qlog:events:quic",
                "urn:ietf:params:qlog:events:http3"
            ]
        }
    }

    output_entries = []
    for event in trace.get("events", []):
        output_entry = {
            "time": event.get("time", 0),
            "name": event.get("name", "unknown"),
            "data": event.get("data", {})
        }
        output_entries.append(output_entry)

    with open(output_file, "wb") as outfile:
        # Write RS + header JSON
        outfile.write(b"\x1E" + json.dumps(header).encode("utf-8") + b"\n")
        # Write RS + each event
        for entry in output_entries:
            outfile.write(b"\x1E" + json.dumps(entry).encode("utf-8") + b"\n")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python script.py <input_json_file> <output_jsonseq_file>")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    convert_json_to_jsonseq(input_path, output_path)
    print(f"Conversion complete. Output saved to {output_path}")
