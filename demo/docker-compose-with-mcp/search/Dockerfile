# SPDX-License-Identifier: Apache-2.0
# Copyright The Robinauts Authors
#
# Brave's MCP server, which speaks Streamable HTTP itself (no proxy needed).
# Only web search is enabled: the others (news, images, video, local,
# summarizer) would be tool definitions the agents carry for nothing.
FROM node:24-slim

RUN npm install --global --no-fund --no-audit @brave/brave-search-mcp-server@2.1.4

USER node
EXPOSE 8934
CMD ["brave-search-mcp-server", "--transport", "http", "--host", "0.0.0.0", "--port", "8934", \
     "--enabled-tools", "brave_web_search"]
