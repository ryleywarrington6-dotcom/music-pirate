#!/usr/bin/env bash
# exit on error
set -o errexit

# 1. Download and unpack a static build of ffmpeg
echo "Downloading ffmpeg..."
mkdir -p ffmpeg_bin
curl -L https://johnvansickle.com | tar -xJ --strip-components=1 -C ffmpeg_bin

# 2. Add ffmpeg to the system PATH temporarily for the build process
export PATH="$(pwd)/ffmpeg_bin:$PATH"

# 3. Install Python dependencies
echo "Installing dependencies..."
if [ -f "poetry.lock" ]; then
    poetry install
else
    pip install -r requirements.txt
fi
