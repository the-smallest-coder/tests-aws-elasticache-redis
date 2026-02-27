FROM python:3.11-slim

WORKDIR /app

# Copy and install dependencies with pinned versions
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all report-generator modules
COPY helpers.py parsers.py charts.py cards.py template.py report_generator.py ./

# Run the report generator
CMD ["python", "report_generator.py"]
