#!/bin/bash
set -e

echo "=== Running Preview Image Tests ==="
docker-compose -f docker-compose.test.yml run --rm test

echo ""
echo "=== Copying output to previews/ ==="
mkdir -p previews
cp -r test_output/* previews/ 2>/dev/null || xcopy /E /I /Y test_output previews

echo ""
echo "=== Preview images saved to: previews/ ==="
ls -la previews/
