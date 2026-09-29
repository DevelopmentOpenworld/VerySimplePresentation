# VerySimplePresentation server: Linux, Python 3.12 (standard library only), Node 20 with Playwright and Chromium for the rendering
# check and the PDF, the open fonts of profiles/mvp/fonts.json.
#   docker build -t verysimplepresentation .
#   docker build -t verysimplepresentation --build-arg MS_CORE_FONTS_EULA=accept .   # Microsoft core fonts: you accept their EULA
#   docker run -p 8770:8770 -e VSP_LLM_API_KEY -v verysimplepresentation-runs:/app/var/generator verysimplepresentation
FROM node:20-bookworm-slim AS node

FROM python:3.12-slim-bookworm
ARG MS_CORE_FONTS_EULA=no
ENV DEBIAN_FRONTEND=noninteractive PYTHONIOENCODING=utf-8
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Microsoft core fonts (Arial Black, Verdana, Trebuchet MS, Georgia, ...) only on an explicit acceptance of their EULA
# (owner answer Р3 (а)); the Debian installer downloads them from Microsoft's original packages.
RUN if [ "$MS_CORE_FONTS_EULA" = "accept" ]; then \
      echo "deb http://deb.debian.org/debian bookworm contrib" > /etc/apt/sources.list.d/contrib.list \
      && apt-get update \
      && echo "ttf-mscorefonts-installer msttcorefonts/accept-mscorefonts-eula select true" | debconf-set-selections \
      && apt-get install -y --no-install-recommends ttf-mscorefonts-installer \
      && rm -rf /var/lib/apt/lists/*; fi
WORKDIR /app
COPY apps/generator/package.json apps/generator/package.json
RUN cd apps/generator && npm install --omit=dev && npx playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY . .
# The open fonts are downloaded here, pinned to the commit of the lock and checked file by file; never while generating.
RUN python -X utf8 -B scripts/fetch_fonts.py fetch substitutes --dest /app/var/fonts \
 && if [ -d /usr/share/fonts/truetype/msttcorefonts ]; then python -X utf8 -B scripts/fetch_fonts.py index --dest /usr/share/fonts/truetype/msttcorefonts; fi
EXPOSE 8770
CMD ["python", "-X", "utf8", "-B", "apps/generator/server.py", "--config", "config.docker.json"]
