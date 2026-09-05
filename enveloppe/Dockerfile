ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python et pip viennent de l'image de base (voir build.yaml) : rien à
# installer, donc rien qui puisse varier d'une révision d'Alpine à l'autre.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUDGET_DATA_DIR=/data

WORKDIR /srv/enveloppe

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
