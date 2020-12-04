Creating Required RAM Disk
==========================

The goal is to have a 100MB directory in RAM for temporarily storage.

1) create the tmp dir

> sudo mkdir /var/ramdisk

2) edit the fstab file by

> sudo vi /etc/fstab

3) add the following line, then save the file:

tmpfs /var/ramdisk tmpfs nodev,nosuid,size=100M 0 0 

4) mount the new drive

> sudo mount -a

5) Check by issuing:

> df

Should return a number of lines including:
Filesystem     1K-blocks    Used Available Use% Mounted on
  :                   :        :       :      :     :
tmpfs             102400       0    102400   0% /var/ramdisk


Enabling the initialization at boot - needed for cases where the watchdog resets the system
===========================================================================================

Copy DogDetect.service into /etc/systemd/system as root, for example:

> sudo cp DogDetect.service /etc/systemd/system/DogDetect.service

Once this has been copied, you can attempt to start the service using the following command:

> sudo systemctl start DogDetect.service

Stop it using following command:

sudo systemctl stop DogDetect.service

To have it start automatically on reboot by using this command:

sudo systemctl enable DogDetect.service

The systemctl command can also be used to restart the service or disable it