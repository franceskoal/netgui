# netgui
Web-based network interface management with WAN simulation capabilities using NETEM and Traffic Control (TC).

Debian-Based Distribution Installation:


sudo su
apt update && apt install -y curl unzip
curl -L https://github.com/franceskoal/netgui/archive/refs/heads/main.zip -o netgui.zip && \
unzip netgui.zip && \
cd netgui-main && \
chmod +x setup.sh && \
./setup.sh

RHEL-Based Distribution Installation:


sudo su
dnf install -y curl unzip && \
curl -L https://github.com/franceskoal/netgui/archive/refs/heads/main.zip -o netgui.zip && \
unzip netgui.zip && \
cd netgui-main && \
chmod +x setup.sh && \
./setup.sh


Manual installation:
1. Upload `setup.sh`, `app.py`, and `tailwind.min.css` to `/home/<userprofile>`.
2. Obtain root privileges, make `setup.sh` executable, and run the setup script:

sudo su
chmod +x setup.sh
./setup.sh
