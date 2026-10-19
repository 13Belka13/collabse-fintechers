# Apify python-empty template base image.
FROM apify/actor-python:3.14

USER myuser

COPY --chown=myuser:myuser requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && pip install -r requirements.txt \
 && pip freeze

COPY --chown=myuser:myuser . ./

RUN python -m compileall -q my_actor/

CMD ["python", "-m", "my_actor"]
