FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY report.py .

# Entrypoint to run the report.
# Note: In the ECS task definition override, we might need to
# download the latest report.py from S3 if we want to support dynamic updates,
# but for a baked image, this is sufficient.
CMD ["python", "report.py"]
