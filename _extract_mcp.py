import sys
import re
import json

src = r'C:\Users\donal\.claude\projects\C--Users-donal-projects-SText\52824a5e-e4b0-41c8-8ef5-b6f5fb43b545\tool-results\mcp-firecrawl-firecrawl_scrape-1782023286012.txt'
out_raw = r'C:\Users\donal\projects\SText\_mcp_server_raw.txt'

with open(src, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

data = json.loads(content)
markdown = data['markdown']

# The code section starts at the module docstring
start_marker = '"""MCP socket server for Sublime Text integration."""'
start_idx = markdown.find(start_marker)

# End marker
end_marker = "You can�'t perform that action at this time."
end_idx = markdown.find("You can", start_idx)
if end_idx == -1:
    end_idx = len(markdown)

code_section = markdown[start_idx:end_idx]

# The structure is: each line of source code is on its own paragraph (separated by \n\n)
# Line numbers appear as standalone numeric paragraphs
# Let's split by \n\n and filter out pure line numbers
paragraphs = code_section.split('\n\n')
print(f'Total paragraphs: {len(paragraphs)}')
print('First 20 paragraphs:')
for i, p in enumerate(paragraphs[:20]):
    print(f'  [{i}] {repr(p)}')
