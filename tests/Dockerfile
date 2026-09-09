FROM python:3.12-slim
RUN pip install --no-cache-dir 'pyiceberg[pyarrow]==0.10.0' 'httpx==0.28.1'
COPY examples/smoke.py /smoke.py
CMD ["python", "/smoke.py"]
