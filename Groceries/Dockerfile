# Use official Python image
FROM python:3.10.7

# Set working directory
WORKDIR /app

RUN pip install --upgrade pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Run the script
CMD ["python", "collector.py"]
