#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "=================================================="
echo "Starting deployment via enterprise /opt path..."
echo "=================================================="

# MOVED: Switched destination to /opt/app to natively comply with SELinux policies
TARGET_DIR="/opt/app"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Distro Detection & Correct System Package Installation
if [ -x "$(command -v apt-get)" ]; then
    echo "--> Detected Debian/Ubuntu/Kali system. Installing..."
    apt-get update -y
    apt-get install -y curl python3 python3-pip python3-venv python3-psutil
elif [ -x "$(command -v dnf)" ]; then
    echo "--> Detected Rocky/Fedora system. Installing..."
    dnf install -y curl python3 python3-pip python3-devel
    dnf install -y python3-psutil || true
else
    echo "ERROR: Unsupported package manager. Use apt or dnf."
    exit 1
fi

# 2. Directory Creation
echo "--> Creating target directory..."
mkdir -p "$TARGET_DIR"

# 3. Handle app.py Copying
if [ -f "$SCRIPT_DIR/app.py" ]; then
    echo "--> Copying app.py to $TARGET_DIR..."
    cp "$SCRIPT_DIR/app.py" "$TARGET_DIR/app.py"
elif [ -f "/home/fortinet/app.py" ]; then
    echo "--> Found app.py in /home/fortinet, copying..."
    cp "/home/fortinet/app.py" "$TARGET_DIR/app.py"
else
    echo "WARNING: app.py not found. Creating placeholder."
    echo -e "from flask import Flask\napp = Flask(__name__)\n@app.route('/')\ndef home(): return 'Running!'\nif __name__ == '__main__': app.run(host='0.0.0.0', port=5000)" > "$TARGET_DIR/app.py"
fi

# Clean any Windows line endings out of app.py that cause 203/EXEC errors
if command -v sed >/dev/null 2>&1; then
    sed -i 's/\r$//' "$TARGET_DIR/app.py"
fi

# 4. Handle tailwind.min.css Copying
if [ -f "$SCRIPT_DIR/tailwind.min.css" ]; then
    echo "--> Copying tailwind.min.css to $TARGET_DIR..."
    cp "$SCRIPT_DIR/tailwind.min.css" "$TARGET_DIR/tailwind.min.css"
elif [ -f "/home/fortinet/tailwind.min.css" ]; then
    echo "--> Found tailwind.min.css in /home/fortinet, copying..."
    cp "/home/fortinet/tailwind.min.css" "$TARGET_DIR/tailwind.min.css"
else
    echo "WARNING: tailwind.min.css was not found. Skipping CSS copy."
fi

# 5. Virtual Environment Setup & Requirements
echo "--> Building virtual environment..."
cd "$TARGET_DIR"
python3 -m venv .venv

echo "--> Installing dependencies inside venv..."
"$TARGET_DIR/.venv/bin/pip" install --upgrade pip
"$TARGET_DIR/.venv/bin/pip" install flask python-dotenv watchdog flask_restful colorama uuid psutil

# 6. Execution Permissions & Dynamic SELinux labels
echo "--> Applying execution permissions..."
chmod +x "$TARGET_DIR/app.py"
chmod +x "$TARGET_DIR/.venv/bin/python"

if command -v restorecon >/dev/null 2>&1; then
    echo "--> SELinux detected (Fedora/Rocky). Setting system labels..."
    # Apply standard system file execution context to the /opt path
    chcon -R -t usr_t "$TARGET_DIR" || true
    restorecon -R -v "$TARGET_DIR" || true
fi

# 7. Clean Systemd Service Creation
echo "--> Generating systemd service file..."
SERVICE_FILE="/etc/systemd/system/bwsimulator.service"

echo "[Unit]" > "$SERVICE_FILE"
echo "Description=Flask Application Service" >> "$SERVICE_FILE"
echo "After=network.target" >> "$SERVICE_FILE"
echo "" >> "$SERVICE_FILE"
echo "[Service]" >> "$SERVICE_FILE"
echo "Type=simple" >> "$SERVICE_FILE"
echo "User=root" >> "$SERVICE_FILE"
echo "WorkingDirectory=$TARGET_DIR" >> "$SERVICE_FILE"
echo "ExecStart=$TARGET_DIR/.venv/bin/python $TARGET_DIR/app.py" >> "$SERVICE_FILE"
echo "Restart=always" >> "$SERVICE_FILE"
echo "RestartSec=5" >> "$SERVICE_FILE"
echo "" >> "$SERVICE_FILE"
echo "[Install]" >> "$SERVICE_FILE"
echo "WantedBy=multi-user.target" >> "$SERVICE_FILE"

# 8. Boot Activation
echo "--> Activating and starting backend service..."
systemctl daemon-reload
systemctl enable bwsimulator.service
systemctl restart bwsimulator.service

echo "=================================================="
echo "SUCCESS! Service deployed safely via /opt."
echo "Verify status using: systemctl status bwsimulator.service"
echo "=================================================="