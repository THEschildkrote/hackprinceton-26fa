# isaac's api + the imessage bot, for a cloud host (railway, fly, cloud run...). the host sets PORT.
# the data the live server reads (scans, dossiers, the costar kit) comes in with the build context under cache/;
# the panoramas and point clouds only the offline survey reads stay out (see .dockerignore).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "nudge.photon_bot"]
