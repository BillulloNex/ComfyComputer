FROM kasmweb/ubuntu-jammy-desktop:1.17.0
USER root

ENV HOME=/home/kasm-default-profile
ENV STARTUPDIR=/dockerstartup
WORKDIR $HOME

# 1. System packages & dependencies
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    software-properties-common \
    build-essential \
    libncursesw5-dev \
    libssl-dev \
    libsqlite3-dev \
    libffi-dev \
    gnome-screenshot \
    wmctrl \
    ffmpeg \
    socat \
    xclip \
    x11-utils \
    xdotool \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-dev \
    python3.12-venv \
    python3.12-tk \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 2. Install official Google Chrome Stable (amd64)
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/trusted.gpg.d/google-chrome.gpg \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y google-chrome-stable \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# 3. Configure Python 3.12 as default python3 and install uv
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1 \
    && update-alternatives --set python3 /usr/bin/python3.12 \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12 \
    && pip install uv

# 4. Disable mandatory SSL in KasmVNC (Traefik in Coolify terminates SSL at the edge)
RUN sed -i 's/require_ssl: true/require_ssl: false/g' /usr/share/kasmvnc/kasmvnc_defaults.yaml \
    && sed -i 's/-sslOnly//g' /dockerstartup/vnc_startup.sh

# 5. Enable sudo without password for kasm-user
RUN echo "kasm-user:password" | chpasswd \
    && usermod -aG sudo kasm-user \
    && echo "kasm-user ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# 6. Prepare user directories and fix permissions
RUN chown 1000:0 $HOME \
    && $STARTUPDIR/set_user_permission.sh $HOME
ENV HOME=/home/kasm-user
WORKDIR $HOME
RUN mkdir -p $HOME && chown -R 1000:0 $HOME

# 7. Install Python packages: Cua Computer Server, Cua Agent, browser-use, Playwright
RUN uv pip install --system \
    cua-computer-server \
    "cua-agent[all]" \
    browser-use \
    playwright \
    fastmcp

# 8. Install Playwright browser dependencies (Chromium)
RUN python3.12 -m playwright install --with-deps chromium

# 9. Clean uv cache to reduce image size
RUN rm -rf /home/kasm-user/.cache/uv /root/.cache/uv

# 10. Copy custom startup script
COPY deploy/coolify/custom_startup.sh $STARTUPDIR/custom_startup.sh
RUN chmod +x $STARTUPDIR/custom_startup.sh && chown 1000:0 $STARTUPDIR/custom_startup.sh

USER 1000

# Port 6901: KasmVNC Web Desktop UI
# Port 8000: Cua Computer Server (FastMCP / REST / WebSockets)
# Port 9222: Chrome DevTools Protocol (CDP) for browser-use
EXPOSE 6901 8000 9222
