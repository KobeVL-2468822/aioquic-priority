# to build: sudo docker build -t aioquic-servertest .  # in this directory

# first time run:
sudo docker stop aioquic-servertest && sudo docker rm aioquic-servertest
sudo docker mkdir -p $(pwd)/../results
sudo docker run -it --privileged --cap-add=NET_ADMIN -p 4433:4433/udp --name aioquic --volume=$(pwd)/../results:/srv/aioquic/qlog aioquic-servertest


# after run:
# sudo docker start aioquic-servertest  # from this directory

# sudo docker exec -it aioquic-servertest bash   # (to get back in if you would exit)