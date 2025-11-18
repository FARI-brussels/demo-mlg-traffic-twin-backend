import sys
import xml.etree.ElementTree as ET
from datetime import datetime

def main(filename):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()

        # Try parsing the XML
        try:
            ET.fromstring(content)
            print("The XML file is well-formed.")
            return
        except ET.ParseError:
            print("XML is not well-formed, attempting to fix...")

        # Ensure it's the correct type of file
        if '<additional' not in content or '<rerouter' not in content:
            raise ValueError("This does not appear to be a valid SUMO rerouter file.")

        # Fix missing </closingReroute> tags by checking each line
        fixed_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("<closingReroute")
                and "</closingReroute>" not in stripped
            ):
                # Add closing tag inline at the end of the line
                line = line.rstrip() + "</closingReroute>"
            fixed_lines.append(line)

        fixed_content = "\n".join(fixed_lines)

        # Validate the fixed XML
        try:
            ET.fromstring(fixed_content)
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(fixed_content)
            print(f"File fixed and saved: {filename}")
        except ET.ParseError:
            print("Fix attempt failed: XML is still malformed.")

    except FileNotFoundError:
        print(f"File not found: {filename}")
    except ValueError as e:
        print(str(e))

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python check_xml.py <filename>")
    else:
        main(sys.argv[1])
