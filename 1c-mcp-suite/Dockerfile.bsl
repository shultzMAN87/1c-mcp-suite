FROM eclipse-temurin:17-jre-jammy

WORKDIR /app

# Зависимости Python для MCP-обёртки
RUN apt-get update && apt-get install -y python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
RUN pip install --no-cache-dir "mcp[cli]>=1.0.0" "fastmcp>=0.4.0" uvicorn starlette

# Скачиваем BSL Language Server
ARG BSL_LS_VERSION=0.28.5
RUN mkdir -p /opt/bsl-language-server && \
    apt-get update && apt-get install -y curl && \
    curl -L -o /opt/bsl-language-server/bsl-ls.jar \
      "https://github.com/1c-syntax/bsl-language-server/releases/download/v${BSL_LS_VERSION}/bsl-language-server-${BSL_LS_VERSION}-exec.jar" && \
    apt-get remove -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

ENV BSL_LS_JAR=/opt/bsl-language-server/bsl-ls.jar

COPY mcp-bsl-checker/server.py /app/server.py
COPY mcp_auth.py /app/mcp_auth.py

EXPOSE 8002

CMD ["python3", "/app/server.py"]
