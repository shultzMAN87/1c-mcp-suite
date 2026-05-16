#!/bin/bash
set -euo pipefail
YAX_VERSION="${YAX_VERSION:-25.12}"
YAX_DIR="${YAX_DIR:-/opt/yaxunit}"
YAX_FILE="$YAX_DIR/yaxunit.cfe"
YAX_URL="https://github.com/bia-technologies/yaxunit/releases/download/${YAX_VERSION}/YAxUnit-${YAX_VERSION}.cfe"

mkdir -p "$YAX_DIR"
[ -s "$YAX_FILE" ] && { echo "Already present, skip"; exit 0; }

if command -v curl >/dev/null; then
    curl -fSL --retry 3 -o "$YAX_FILE" "$YAX_URL"
elif command -v wget >/dev/null; then
    wget -O "$YAX_FILE" "$YAX_URL"
else
    apt-get update && apt-get install -y --no-install-recommends wget ca-certificates
    wget -O "$YAX_FILE" "$YAX_URL"
fi

chmod 644 "$YAX_FILE"
echo "Installed: $(stat -c%s "$YAX_FILE") bytes"
