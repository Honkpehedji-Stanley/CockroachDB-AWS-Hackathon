FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install -r requirements.txt --no-cache-dir

COPY handler.py bedrock_client.py memory.py ingest.py ${LAMBDA_TASK_ROOT}/

COPY cc-ca.crt ${LAMBDA_TASK_ROOT}/cc-ca.crt
CMD ["handler.lambda_handler"]
