import json
import sys


def convert_json_to_jsonseq(input_file, output_file):
    with open(input_file, "r", encoding="utf-8") as infile:
        data = json.load(infile)

    if "qlog_format" not in data or data["qlog_format"] != "JSON":
        raise ValueError("Invalid JSON v0.3 file format")

    output_entries = []
    
    for trace in data.get("traces", []):
        vantage_point = trace.get("vantage_point", {})
        common_fields = trace.get("common_fields", {})
        
        for event in trace.get("events", []):
            output_entry = {
                "time": event.get("time", 0),
                "name": event.get("name", "unknown"),
                "data": event.get("data", {})
            }
            output_entries.append(json.dumps(output_entry))
    
    with open(output_file, "w", encoding="utf-8") as outfile:
        outfile.write(json.dumps({
            "qlog_version": "0.3",
            "qlog_format": "JSON-SEQ",
            "title": "Converted log",
            "description": "Converted from JSON v0.3"
        }) + "\n")
        
        for entry in output_entries:
            outfile.write("\x1E" + entry + "\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python script.py <input_json_file> <output_jsonseq_file>")
        sys.exit(1)
    
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    
    convert_json_to_jsonseq(input_path, output_path)
    print(f"Conversion complete. Output saved to {output_path}")