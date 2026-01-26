FROM debian:trixie-20260112 AS base
LABEL maintainer="Rob Knop <raknop@lbl.gov>"

# These next two are what's needed to run as raknop on NERSC.
# If somebody else is isntalling this, they will need to specify
#   a different UID/GID.  (It's possible they can do this all at runtime
#   with an init-container or something like that.)
ARG UID=95089
ARG GID=45703

SHELL ["/bin/bash", "-c"]

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ="UTC"

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y python3 locales netcat-openbsd net-tools lynx ca-certificates \
                          tmux emacs-nox less \
    && apt-get -y autoremove \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN cat /etc/locale.gen | perl -pe 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' > /etc/locale.gen.new \
    && mv /etc/locale.gen.new /etc/locale.gen
RUN locale-gen en_US.utf8
ENV LANG=en_US.UTF-8
ENV LANGUAGE=en_US:en
ENV LC_ALL=en_US.UTF-8

RUN ln -s /usr/bin/python3 /usr/bin/python
ENV LESS=-XLRi

# ======================================================================
# apt-getting pip installs a full dev environment, which we don't
#   want in our final image.  (400 unnecessary MB.)

FROM base AS build

RUN apt-get update && apt-get install -y python3-pip python3-venv

RUN mkdir /venv
RUN python3 -mvenv /venv

RUN source /venv/bin/activate \
  && pip --no-cache install \
      "flask-session>=0.8.0,<1.0.0" \
      "flask>=3.1.2,<4.0.0" \
      "gevent>=25.9.1,<26.0.0" \
      "gunicorn>=24.1.1,<25.0.0" \
      "pytest-timestamper==0.0.10" \
      "pytest>=8.4.2,<9.0.0" \
      "remote-pdb==2.1.0" \
      "requests>=2.32.5,<3.0.0"

ENTRYPOINT [ "tail", "-f", "/etc/issue" ]

# ======================================================================

FROM base AS final

COPY --from=build /venv/ /venv/
ENV PATH=/venv/bin:$PATH

# Both /secrets and /dest should be bind-mounted at runtime.
# Do NOT use the default values here.
RUN mkdir /secrets
RUN echo "testing testing" >> /secrets/connector_tokens
RUN echo "insecure" >> /secrets/flask_secret_key
RUN mkdir /dest

RUN mkdir /file-archive-server
COPY file_archive_server.py /file-archive-server/file_archive_server.py
ENV PYTHONPATH /file-archive-server

RUN mkdir /sessions

USER $UID:$GID

EXPOSE 8080
ENTRYPOINT [ "/venv/bin/gunicorn", "-b", "0.0.0.0:8080", "-k", "gevent", \
             "--timeout", "300", "--workers", "10", \
             "file_archive_server:application" ]
# ENTRYPOINT [ "/venv/bin/gunicorn", "-b", "0.0.0.0:8080", "-k", "gevent", \
#              "--timeout", "300", "--workers", "1", \
#              "file_archive_server:application" ]
