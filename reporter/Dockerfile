# Pinned to the manifest-list (multi-arch) digest, in sync with
# var.reporter_image's default in variables.tf (WP3, PLAN_2.md;
# tests/test_reporter_dependency_pins.py checks the sync). Re-fetch when a
# bump is deliberate:
#   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
#   curl -sI -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json" https://registry-1.docker.io/v2/library/python/manifests/3.11-slim | grep -i docker-content-digest
FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all report-generator modules
COPY . ./

# Run the report generator
CMD ["python", "report_generator.py"]
