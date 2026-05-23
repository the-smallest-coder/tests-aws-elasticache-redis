FROM python:3.11-slim

WORKDIR /app

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all report-generator modules
COPY . ./

# Run the report generator
CMD ["python", "report_generator.py"]
