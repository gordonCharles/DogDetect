#!/usr/bin/python
'''
This provides a dog / cat detection application running an a raspberry pi with a "Raspberry Pi High Quality Camera",
https://www.raspberrypi.org/products/raspberry-pi-high-quality-camera/ and a Arducam wide anlge (6mm) lens,
https://www.amazon.com/gp/product/B088GWZPL1/.  This application uses the SSD Mobilnet V2 Single-Shot multibox
Detection (SSD) neural network targeting mobile applications, running on OpenCV.  V3 was available during
development; however, there appeared to be incompatibility between the .pbtxt definitions and OpenCVs
implementation and was not adopted.  The goal of the system is as quickly as possible detect the presence
of a dog in our front yard, typically on or near the sidewalk and send a notification to a separate system
on the same subnet.

This also sends SMS notifications (without a SMS service) for free when a dog has been detected and sends an
email with the detection image including the classification regions and their confidence scores.  Targeting a
rPi4, the system uses threading to both maximize the recognition frame rate and avoid messaging and network
delays from impacting the frame rate.  To maximize the frame rate, a digitally locked loop is implemented
where delays are inserted following processing of a frame (in a thread - called a phase in this image) to match
the average delay of the system.  In each phase a captured image runs in its own thread through the object
classification (the single largest CPU hog), such that multiple images have object classification running in
parallel, the results for a phase are dequeued immediately after kicking off the phase + NUMBER_OF_PHASES-1.
The rPi4 has four cores so there is no value in exceeding four phases.  In addition the neural net is IO
limited, so there is diminishing gains in increasing the number of PHASES beyond 3.  The software is configured
to support arbitrary number of phases, so this can be configured based upon any changes ot the hardware.

The camera's viewport has a very wide aspect ratio, reflecting the visual region of interest.  The neural net
is designed around a 1:1 aspect ratio and is tolerant to some variance in aspect ratio, but demonstrated
significant degradation for an aspect ratio of 10:3.  To maximize accuracy motion detection is applied to the
image and the object recognition is run on a 1:1 portion (IMAGE_HEIGHT x IMAGE_HEIGHT) centered on the motions
centroid.  As configured the system has a frame rate of ~2.1 fps.

When running all four cores are typically in the mid 90s% load, so some form of cooling is needed.  Given
the system is running indoors I chose to go with passive cooling and found the Geekworm Raspberry Pi 4 Case,
https://www.amazon.com/gp/product/B07X5Y81C6/ to be very effective.  Unfortunately for my application WiFi
range is important and this case places a large block of Aluminum around the antenna, so I had to modify
the case to create a cutout in the top side metal around the antennas.  Peak memory usage is around 3GB so
the 4GB rPi4 would be sufficient.  The application expects a RAM disk to be setup, @ /var/ramdisk/, to store
a cyclical list of captured images.  This is done to 1) avoid wear on the SD card - SD card writes have a
real impact on the operating life and 2) improve the speed performance when most needed.

Given the raised operating temperature of the system a night mode has been implemented to lower the
power consumption when the image is too poorly illuminated to be useful.  The digital lock loop slows
down by a factor of ~120 in this mode with the goal of preserving energy and extending the life of the
system (heat has an exponential impact on lifespan in almost everything, electronics included).

This application will send an SMS message without requiring a service to do so.  Most (if not all cellular
providers provide an e-mail address to address text messages.  For the common us carriers here is the
address syntax:

Sprint      phonenumber@messaging.sprintpcs.com
Verizon	    phonenumber@vtext.com
T-Mobile	phonenumber@tmomail.net
AT&T	    phonenumber@txt.att.net

This solution is intended to run stand alone indefinitely and uses the watchdog driver which provides
an interface to the Broadcom's watchdog timer.  Unfortunately, when the original Desbian watchdog solution was
implemented the authors created a daemon which would monitor other processes within the system and take
responsibility for either petting a HW watchdog or maintaining a SW monitor in lieu of one, this software was
also named watchdog.  So now configurations for the watchdog all start with WATCHDOG and it is not all obvious
how one tells the configurations apart.  Only one SW instance is allowed to connect to the drive at a time, so
if the daemon "SW watchdog" is enabled that is your only option.  This program directly interacts with the
watchdog driver.  For this to work the watchdog can not be started at the system level.  System level watchdogs
are typically setup to monitor the process IDs of critical infrastructure and can be pointed at process IDs for
a particular application; however, with a multi-threaded application which is adding and deleting threads this
complex at least.  This application as an alternative owns the watchdog function and monitors that the critical
thread(s) remain operating with the assumption that if the critical infrastructure relative to the application
fails the application will fail with it.  The watchdog implementation is such that the watchdog is not enabled
until the application reads the ENABLE_WATCHDOG parameter from a configuration file.  When it does so it writes
a working configuration file to the RAMDISK to allow remote determination of the configuration state.  The
configuration file provides a safety measure for disabling the watchdog, as deleting or renaming the config
file will disable the watchdog the next time the application is started.

To start the application following either a loss of power, including a watchdog event, a systemd service is
created.  See instructions in DogDetect_README.txt for installation, enabling and disabling the service.
'''


# Import packages
import os
import cv2
import numpy as np
import argparse
import smtplib
import ssl
import datetime
import socket
import time
import json
import mimetypes
import imutils
from picamera.array import PiRGBArray
from picamera import PiCamera
from threading import Thread
from time import sleep
from email.message import EmailMessage
from email.utils import make_msgid
from mscoco_label_map import category_map, category_index
'''
Imports from private are constants that need to be created for a specific userID.  You may also wish
to change the constant name GORDONS_EMAIL (unless your name is Gordon ;).  For obvious reasons private.py
is not included in the repo.
'''
from private import SUDO_PASSWORD as SUDO_PASSWORD  # Sudo password needed to pet the watchdog
from private import SENDER_EMAIL as SENDER_EMAIL    # E-mail account to use for sending e-mail notifications
from private import PASSWORD as PASSWORD            # Password for email acount to send notificaitons
from private import GORDONS_EMAIL as GORDONS_EMAIL  # Recipient email address
''' Recipient's cell phone numbers e-mail address
NOTE: You can e-mail a text message to a cell phone at no cost, at least for the major carriers in the US.
Each carrier has it's own format for the e-mail address as shown in the following example:

Provider	Format		                            Example Number: 4081234567
======================================================================================
Sprint	    phonenumber@messaging.sprintpcs.com     4081234567@messaging.sprintpcs.com
Verizon	    phonenumber@vtext.com	            	4081234567@vtext.com
T-Mobile	phonenumber@tmomail.net	            	4081234567@tmomail.net
AT&T	    phonenumber@txt.att.net	            	4081234567@txt.att.net

The repository hosting this file will include an excel spreadsheet which generates the e-mail addresses 
from the 10-digit number.
'''
from private import GORDONS_CELL as GORDONS_CELL    # Recipient's cell phone numbers e-mail address

DEBUG      = False
DISPLAY_ON = False

# Need to know what system we are running on, especially regarding watchdog and for convenience not use a ramdisk
osinfo = os.uname()
if osinfo[1] == 'raspberrypi':
    piHost = True

if piHost:
    RAM_DISK           = '/var/ramdisk/'
else:
    RAM_DISK           = '/tmp/'

parser = argparse.ArgumentParser()
parser.add_argument('--serviceMode', help='does not display images - use when running as a daemon service',
                    action='store_true')
args = parser.parse_args()

'''
To ensure we can quickly turn off the watch dog mode should an issue be introduced, the 
watchdog petting, which enables the watchdog the first time it is performed, must be 
enabled by the presense of the ENABLE_WATCHDOG flag being set to 1.  Moving or deleting
the config file will prevent the watdog from being run the next time the script is called.
This is useful to be able to disable the watchdog and then take the execution out of 
service mode.
'''
WATCH_DOG_ENABLE       = False
CONFIG_FILE            = "/home/pi/Software/Python/DogDetect2/sc_config.txt"
if os.path.isfile(CONFIG_FILE):
    workingConfigFile = open(RAM_DISK + 'working_config.txt', "w")
    with open(CONFIG_FILE) as configFile:
        for line in configFile:
            workingConfigFile.write(line)
            parameter = line.rstrip()
            if parameter == "ENABLE_WATCHDOG=1":
                WATCH_DOG_ENABLE = True
    workingConfigFile.close()
else:
    print("Running without a config file")

WATCH_DOG_PET_INTERVAL = 5 # Watch Dog Petting interval (must be less than 15 seconds or system will reboot
keepAlive              = 0

SSL_PORT          = 465  # For SSL
GMAIL_SMTP_SERVER = "smtp.gmail.com"

CYAN  = (255, 255, 0)
RED   = (0,   0,   255)
BLUE  = (255, 0,   0)
WHITE = (255, 255, 255)

# Times in seconds
TIME_BETWEEN_MESSAGES  = 30
ANIMAL_DETECT_DEBOUNCE = 10
IMAGE_FILE             = RAM_DISK + 'Current.jpg'


# Set up camera constants
FULL_RES_WIDTH            = 4056
FULL_RES_HEIGHT           = 3040
IM_WIDTH                  = 2032
IM_HEIGHT                 = 608
scale_percent             = 25 * int(5000 / IM_WIDTH)  # percent of original size for displayed image
FILTER_SCALE              = 4 # scaling applied on image prior to focus detection.
FOCUS_SENSITIVITY         = 70 # Value on 100 point scale for degree of sensitivity, higher being more sensitive.
FOCUS_THRESHOLD           = 40 # Threshold value from 0 to 255, determining the change of intensity to qualify as movement
PHASES                    = 3
NIGHT_MODE_ON_THRESH      = 4
NIGHT_MODE_OFF_THRESH     = 16
NIGHT_MODE_FRAME_SLOWDOWN = 60

#### Initialize NN model ####

# Name of the directory containing the object detection module we're using
PATH_TO_PROTO_BINARY = "/home/pi/Software/Python/DogDetect2/ssd_mobilenet_v2_frozen_inference_graph.pb"
PATH_TO_PROTO_TEXT   = "/home/pi/Software/Python/DogDetect2/ssd_mobilenet_v2_coco_2018_03_29.pbtxt"
V2_BLOB_SIZE         = 300
#PATH_TO_PROTO_BINARY = "/home/pi/Software/Python/DogDetect2/ssd_mobilenet_v3_frozen_inference_graph.pb"
#PATH_TO_PROTO_TEXT   = "/home/pi/Software/Python/DogDetect2/ssd_mobilenet_v3_large_coco_2020_01_14.pbtxt"
V3_BLOB_SIZE         = 320

BLOB_SIZE = V2_BLOB_SIZE

# Create an NN instance for each phase
cvNet = [cv2.dnn.readNetFromTensorflow(PATH_TO_PROTO_BINARY, PATH_TO_PROTO_TEXT) for _ in range(PHASES)]

#Calculate the focus detection sensitivity converting from a 100 point scale to a fraction of the detection area:
focusSensitivity = int(50 / (FOCUS_SENSITIVITY * FOCUS_SENSITIVITY) * (IM_HEIGHT * IM_WIDTH) / (FILTER_SCALE * FILTER_SCALE))

categories     = [{'id': 1, 'name': 'person'}, {'id': 2, 'name': 'bicycle'}, {'id': 3, 'name': 'car'}, {'id': 4, 'name': 'motorcycle'}, {'id': 5, 'name': 'airplane'}, {'id': 6, 'name': 'bus'}, {'id': 7, 'name': 'train'}, {'id': 8, 'name': 'truck'}, {'id': 9, 'name': 'boat'}, {'id': 10, 'name': 'traffic light'}, {'id': 11, 'name': 'fire hydrant'}, {'id': 13, 'name': 'stop sign'}, {'id': 14, 'name': 'parking meter'}, {'id': 15, 'name': 'bench'}, {'id': 16, 'name': 'bird'}, {'id': 17, 'name': 'cat'}, {'id': 18, 'name': 'dog'}, {'id': 19, 'name': 'horse'}, {'id': 20, 'name': 'sheep'}, {'id': 21, 'name': 'cow'}, {'id': 22, 'name': 'elephant'}, {'id': 23, 'name': 'bear'}, {'id': 24, 'name': 'zebra'}, {'id': 25, 'name': 'giraffe'}, {'id': 27, 'name': 'backpack'}, {'id': 28, 'name': 'umbrella'}, {'id': 31, 'name': 'handbag'}, {'id': 32, 'name': 'tie'}, {'id': 33, 'name': 'suitcase'}, {'id': 34, 'name': 'frisbee'}, {'id': 35, 'name': 'skis'}, {'id': 36, 'name': 'snowboard'}, {'id': 37, 'name': 'sports ball'}, {'id': 38, 'name': 'kite'}, {'id': 39, 'name': 'baseball bat'}, {'id': 40, 'name': 'baseball glove'}, {'id': 41, 'name': 'skateboard'}, {'id': 42, 'name': 'surfboard'}, {'id': 43, 'name': 'tennis racket'}, {'id': 44, 'name': 'bottle'}, {'id': 46, 'name': 'wine glass'}, {'id': 47, 'name': 'cup'}, {'id': 48, 'name': 'fork'}, {'id': 49, 'name': 'knife'}, {'id': 50, 'name': 'spoon'}, {'id': 51, 'name': 'bowl'}, {'id': 52, 'name': 'banana'}, {'id': 53, 'name': 'apple'}, {'id': 54, 'name': 'sandwich'}, {'id': 55, 'name': 'orange'}, {'id': 56, 'name': 'broccoli'}, {'id': 57, 'name': 'carrot'}, {'id': 58, 'name': 'hot dog'}, {'id': 59, 'name': 'pizza'}, {'id': 60, 'name': 'donut'}, {'id': 61, 'name': 'cake'}, {'id': 62, 'name': 'chair'}, {'id': 63, 'name': 'couch'}, {'id': 64, 'name': 'potted plant'}, {'id': 65, 'name': 'bed'}, {'id': 67, 'name': 'dining table'}, {'id': 70, 'name': 'toilet'}, {'id': 72, 'name': 'tv'}, {'id': 73, 'name': 'laptop'}, {'id': 74, 'name': 'mouse'}, {'id': 75, 'name': 'remote'}, {'id': 76, 'name': 'keyboard'}, {'id': 77, 'name': 'cell phone'}, {'id': 78, 'name': 'microwave'}, {'id': 79, 'name': 'oven'}, {'id': 80, 'name': 'toaster'}, {'id': 81, 'name': 'sink'}, {'id': 82, 'name': 'refrigerator'}, {'id': 84, 'name': 'book'}, {'id': 85, 'name': 'clock'}, {'id': 86, 'name': 'vase'}, {'id': 87, 'name': 'scissors'}, {'id': 88, 'name': 'teddy bear'}, {'id': 89, 'name': 'hair drier'}, {'id': 90, 'name': 'toothbrush'}]
category_index = {1: {'id': 1, 'name': 'person'}, 2: {'id': 2, 'name': 'bicycle'}, 3: {'id': 3, 'name': 'car'}, 4: {'id': 4, 'name': 'motorcycle'}, 5: {'id': 5, 'name': 'airplane'}, 6: {'id': 6, 'name': 'bus'}, 7: {'id': 7, 'name': 'train'}, 8: {'id': 8, 'name': 'truck'}, 9: {'id': 9, 'name': 'boat'}, 10: {'id': 10, 'name': 'traffic light'}, 11: {'id': 11, 'name': 'fire hydrant'}, 13: {'id': 13, 'name': 'stop sign'}, 14: {'id': 14, 'name': 'parking meter'}, 15: {'id': 15, 'name': 'bench'}, 16: {'id': 16, 'name': 'bird'}, 17: {'id': 17, 'name': 'cat'}, 18: {'id': 18, 'name': 'dog'}, 19: {'id': 19, 'name': 'horse'}, 20: {'id': 20, 'name': 'sheep'}, 21: {'id': 21, 'name': 'cow'}, 22: {'id': 22, 'name': 'elephant'}, 23: {'id': 23, 'name': 'bear'}, 24: {'id': 24, 'name': 'zebra'}, 25: {'id': 25, 'name': 'giraffe'}, 27: {'id': 27, 'name': 'backpack'}, 28: {'id': 28, 'name': 'umbrella'}, 31: {'id': 31, 'name': 'handbag'}, 32: {'id': 32, 'name': 'tie'}, 33: {'id': 33, 'name': 'suitcase'}, 34: {'id': 34, 'name': 'frisbee'}, 35: {'id': 35, 'name': 'skis'}, 36: {'id': 36, 'name': 'snowboard'}, 37: {'id': 37, 'name': 'sports ball'}, 38: {'id': 38, 'name': 'kite'}, 39: {'id': 39, 'name': 'baseball bat'}, 40: {'id': 40, 'name': 'baseball glove'}, 41: {'id': 41, 'name': 'skateboard'}, 42: {'id': 42, 'name': 'surfboard'}, 43: {'id': 43, 'name': 'tennis racket'}, 44: {'id': 44, 'name': 'bottle'}, 46: {'id': 46, 'name': 'wine glass'}, 47: {'id': 47, 'name': 'cup'}, 48: {'id': 48, 'name': 'fork'}, 49: {'id': 49, 'name': 'knife'}, 50: {'id': 50, 'name': 'spoon'}, 51: {'id': 51, 'name': 'bowl'}, 52: {'id': 52, 'name': 'banana'}, 53: {'id': 53, 'name': 'apple'}, 54: {'id': 54, 'name': 'sandwich'}, 55: {'id': 55, 'name': 'orange'}, 56: {'id': 56, 'name': 'broccoli'}, 57: {'id': 57, 'name': 'carrot'}, 58: {'id': 58, 'name': 'hot dog'}, 59: {'id': 59, 'name': 'pizza'}, 60: {'id': 60, 'name': 'donut'}, 61: {'id': 61, 'name': 'cake'}, 62: {'id': 62, 'name': 'chair'}, 63: {'id': 63, 'name': 'couch'}, 64: {'id': 64, 'name': 'potted plant'}, 65: {'id': 65, 'name': 'bed'}, 67: {'id': 67, 'name': 'dining table'}, 70: {'id': 70, 'name': 'toilet'}, 72: {'id': 72, 'name': 'tv'}, 73: {'id': 73, 'name': 'laptop'}, 74: {'id': 74, 'name': 'mouse'}, 75: {'id': 75, 'name': 'remote'}, 76: {'id': 76, 'name': 'keyboard'}, 77: {'id': 77, 'name': 'cell phone'}, 78: {'id': 78, 'name': 'microwave'}, 79: {'id': 79, 'name': 'oven'}, 80: {'id': 80, 'name': 'toaster'}, 81: {'id': 81, 'name': 'sink'}, 82: {'id': 82, 'name': 'refrigerator'}, 84: {'id': 84, 'name': 'book'}, 85: {'id': 85, 'name': 'clock'}, 86: {'id': 86, 'name': 'vase'}, 87: {'id': 87, 'name': 'scissors'}, 88: {'id': 88, 'name': 'teddy bear'}, 89: {'id': 89, 'name': 'hair drier'}, 90: {'id': 90, 'name': 'toothbrush'}}

#### Initialize other parameters ####

# Initialize frame rate calculation
frame_rate_calc = 1
freq            = cv2.getTickFrequency()
font            = cv2.FONT_HERSHEY_SIMPLEX
frameCount      = 0

# Initialize control variables used for pet detector
catOrDogSeen     = TIME_BETWEEN_MESSAGES * freq
catOrDogLastSeen = 0
imageLastSent    = 0
imageCapture     = False

def sendTextMessage(messageSubject, messageText, recipient):
    ''' 
    Sends text message (as an outbound e-mail with subject and body defined by messageText to 
    recipient.

    Args:
        messageSubject (string): message subjet.
        messageText (string): message content
        recipient (string): phone number formated as e-mail address - carrier specific format

    Returns:
        Nothing
    '''
    message = "From: %s\r\n" % SENDER_EMAIL \
              + "To: %s\r\n" % recipient \
              + "Subject: %s\r\n" % messageSubject \
              + "\r\n" \
              + messageText

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_SERVER, SSL_PORT, context=context) as server:
        server.login(SENDER_EMAIL, PASSWORD)
        server.sendmail(SENDER_EMAIL, recipient, message)
    print("Message Sent")


def sendEmailWithImage(image, subject, message_text, recipient):
    ''' 
    Sends an email with subject and body defined by textFile to recipient.

    Args:
        image (cv2.img): image to be attached to e-mail
        subject (string): message subjet.
        textFile (string): pointer to text file
        recipient (string): e-mail address of recipient

    Returns:
        Nothing
    '''
    # generic email headers
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = recipient

    # set the plain text body
    msg.set_content(' ')  # this is required, but does not appear in messages?

    # now create a Content-ID for the image
    image_cid = make_msgid(domain='dogdetect.com')
    # if `domain` argument isn't provided, it will use your computer's name

    # set an alternative html body
    escape_cid = "{image_cid}"
    message_content = f"""\
    <html>
        <body>
            <p> {message_text}
            </p>
            <img src="cid:{escape_cid}">
        </body>
    </html>
    """
    msg.add_alternative(message_content.format(image_cid=image_cid[1:-1]), subtype='html')
    # image_cid looks like <long.random.number@xyz.com>
    # to use it as the img src, we don't need `<` or `>`
    # so we use [1:-1] to strip them off

    # now open the image and attach it to the email
    with open(IMAGE_FILE, 'rb') as img:
        # know the Content-Type of the image
        maintype, subtype = mimetypes.guess_type(img.name)[0].split('/')
        # attach it
        msg.get_payload()[1].add_related(img.read(),
                                         maintype=maintype,
                                         subtype=subtype,
                                         cid=image_cid)
    text = msg.as_string()
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(GMAIL_SMTP_SERVER, SSL_PORT, context=context) as server:
        server.login(SENDER_EMAIL, PASSWORD)
        server.sendmail(SENDER_EMAIL, recipient, text)
    print("Email Sent")

def turnOnSprinklers():
    ''' 
    Thread to send a JSON message, turning on the sprinklers.

    Returns:
        Nothing
    '''
    HOST            = '192.168.1.244'  # Sprinkler Controller's IP address
    PORT            = 2579             # The port used by the server
    MAX_TCP_RETRIES = 10
    TCP_TIMEOUT     = 0.5

    received = {}
    for i in range(MAX_TCP_RETRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sprinkler_socket:
            sprinkler_socket.connect((HOST, PORT))
            sprinkler_socket.settimeout(TCP_TIMEOUT)
            currentTime = datetime.datetime.now()
            textTime = currentTime.strftime("%-I:%M%p")
            msgDict = {"Type": "Dog Warning", "Time": textTime, "Count": i}
            message = json.dumps(msgDict)
            sprinkler_socket.sendall(message.encode('utf-8'))
            try:
                data = sprinkler_socket.recv(1024)
                received = json.loads(data)
                print('Received', f' {i} ', received)
            except:
                print("exception")
                received['Type'] = "Exception"
        if received['Type'] == "Dog Warning Ack":
            break
        if i == MAX_TCP_RETRIES - 1:
            messageSubject = f"Failed to connect with sprinkler after {MAX_TCP_RETRIES} attempts."
            messageText    = f'There is a dog in the front yard! {currentTime.strftime("%x %I:%M:%S %p")}'
            try:
                sendFailureNotice = Thread(target= sendTextMessage, args=(messageSubject, messageText, GORDONS_CELL))
                sendFailureNotice.start()
            except:
                print("Error: unable to start sendFailureNotice thread")


def notify(messageSubject, messageText, frame):
    ''' 
    Thread created to isolate the slower task of communicating and writing image to a file.

    Returns:
        Nothing
    '''
    sendTextMessage(messageSubject, messageText, GORDONS_CELL)
    cv2.imwrite(IMAGE_FILE, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    sendEmailWithImage(IMAGE_FILE, messageSubject, messageText, GORDONS_EMAIL)


def object_detector(frame, cvNet):
    '''
    This function detects motion then crops the image to the center of the motion prior to running the 
    object recognition neural net.  Object recognition performs best when the provided image has a 1:1
    aspect ratio.  The region of interest has been trimmed at the camera interface in the vertical 
    dimension, so the fraction of the image used for object recognition is of the dimension 
    IMAGE_HEIGHT x IMAGE_HEIGHT. 

    Args:
        image (frame): image captured from camera
        cvNet (cv2 NN): object dectecition trained neural network 

    Returns:
        Nothing
    '''
    # Use globals for the control variables so they retain their value after function exits
    global frameCount
    global catOrDogSeen, catOrDogLastSeen, imageLastSent
    global imageCapture
    global referenceFrame
    global referenceFrameTime, lastActiveTime
    global refX, refY, refW, refH
    global dogImageCount, catImageCount

    # Motion Detection to determine where in the full image to run object detection 
    grayFrame = imutils.resize(frame, width=int(IM_WIDTH / FILTER_SCALE))
    grayFrame = cv2.cvtColor(grayFrame, cv2.COLOR_BGR2GRAY)
    grayFrame = cv2.GaussianBlur(grayFrame, (41, 41), 0)

    referenceFrameAge = (cv2.getTickCount() - referenceFrameTime) / freq
    if frameCount < 1 or referenceFrameAge > 30:
        referenceFrame = grayFrame
        referenceFrameTime = cv2.getTickCount()

    frameDelta = cv2.absdiff(referenceFrame, grayFrame)
    thresh = cv2.threshold(frameDelta, FOCUS_THRESHOLD, 255, cv2.THRESH_BINARY)[1]
    # dilate the thresholded image to fill in holes, then find contours on thresholded image
    thresh = cv2.dilate(thresh, None, iterations=2)
    cnts = cv2.findContours(thresh.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = imutils.grab_contours(cnts)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:1]
    for contour in cnts:
        if cv2.contourArea(contour) > focusSensitivity:
            #print(f"Contour Size : {cv2.contourArea(contour)}")
            (refX, refY, refW, refH) = cv2.boundingRect(contour)
            lastActiveTime = cv2.getTickCount()

    # Use resulting focus to defne search region in input image 
    searchWidth  = int(IM_HEIGHT * 1.0)
    searchOffset = max(0, int((refX + refW/2) * FILTER_SCALE - searchWidth/2))
    boxes        = []
    scores       = []
    classes      = []

    searchScale  = IM_WIDTH / searchWidth
    processFrame = frame[0:IM_HEIGHT, searchOffset:searchOffset + searchWidth] # format y0:y1, x0:x1

    rows = frame.shape[0]
    cols = frame.shape[1]
    # Run forward pass on object detection NN
    cvNet.setInput(cv2.dnn.blobFromImage(processFrame, size=(BLOB_SIZE, BLOB_SIZE), swapRB=True, crop=False))
    cvOut = cvNet.forward()
    # Draw a box around the searched area
    cv2.rectangle(frame, (searchOffset, 3), (searchOffset + searchWidth, IM_HEIGHT - 3), WHITE, thickness=5)

    # Draw boxes and add labels around objects of interest
    for detection in cvOut[0,0,:,:]:
        score = float(detection[2])
        if score > 0.3:
            left   = int(detection[3] * cols / searchScale + searchOffset)
            top    = int(detection[4] * rows)
            right  = int(detection[5] * cols / searchScale + searchOffset)
            bottom = int(detection[6] * rows)
            boxes.append([top, left, bottom, right])
            scores.append(score)
            idx = int(detection[1])
            classes.append(idx)
            label = "{}: {:.0f}%".format(category_map[idx], score * 100)
            y = top - 15 if top - 15 > 15 else top + 15
            cv2.putText(frame, label, (left, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, BLUE, 2)
            cv2.rectangle(frame, (int(left), int(top)), (int(right), int(bottom)), (23, 230, 210), thickness=5)

    '''
    Check the class of the top detected object by looking at classes[0][0].
    If the top detected object is a cat (17) or a dog (18) (or a teddy bear (88) for test purposes),
    find its center coordinates by looking at the boxes[0][0] variable.
    boxes[0][0] variable holds coordinates of detected objects as (ymin, xmin, ymax, xmax)
    '''
    
    cat      = False
    dog      = False
    catOrDog = False
    if DEBUG: catOrDog = True
    frameCount += 1
    for i in range(len(classes)):
        if scores[i] > 0.5:
            if int(classes[i]) == 17 or int(classes[i]) == 18:
                catOrDog = True
                x = int(((boxes[i][1] + boxes[i][3]) / 2))
                y = int(((boxes[i][0] + boxes[i][2]) / 2))
                cv2.circle(frame, (x, y), 25, (75, 13, 180), -1)
            if int(classes[i]) == 17:
                cat = True
            if int(classes[i]) == 18:
                dog = True
    if len(classes) > 0:
        if int(classes[0]) == 17 or int(classes[0]) == 19 or int(classes[0]) == 88:
            x = int(((boxes[0][1] + boxes[0][3]) / 2))
            y = int(((boxes[0][0] + boxes[0][2]) / 2))

            # Draw a circle at center of object
            cv2.circle(frame, (x, y), 5, (75, 13, 180), -1)

    if catOrDog == True:
        if dog and cat:
            messageObject = "Dog and Cat"
        elif dog:
            messageObject = "Dog"
        else:
            messageObject = "Cat"
        catOrDogLastSeen = catOrDogSeen
        catOrDogSeen = cv2.getTickCount()
        print(f"Time from last image sent:      {(catOrDogSeen-imageLastSent)/freq:.2f}")
        print(f"Time required between messages:{TIME_BETWEEN_MESSAGES}")
        if (catOrDogSeen - imageLastSent) > TIME_BETWEEN_MESSAGES * freq:
            print("Sending Message")

            try:
                sprinklerThread = Thread(target= turnOnSprinklers)
                sprinklerThread.start()
            except:
                print("Error: unable to start sprinkler thread")

            imageLastSent = catOrDogSeen
            
            messageSubject = f"{messageObject} Detected"

            currentRealTime = datetime.datetime.now()
            messageText = f'There is a {messageObject} in the front yard! {currentRealTime.strftime("%x %I:%M:%S %p")}'

            try:
                sendNotice = Thread(target= notify, args=(messageSubject, messageText, frame))
                sendNotice.start()
            except:
                print("Error: unable to start sendNotice thread")
        else:
            imageFileName = f"{RAM_DISK}{messageObject}_{dogImageCount}.jpg"
            cv2.imwrite(imageFileName, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if dog:
                dogImageCount = (dogImageCount + 1) % 100
            else:
                catImageCount = (catImageCount + 1) % 100

    if imageCapture == True:
        messageSubject  = "Manual Image Captured"
        currentRealTime = datetime.datetime.now()
        messageText     = f'Manual image capture @ {currentRealTime.strftime("%x %I:%M:%S %p")}'
        try:
            sendNotice = Thread(target=notify, args=(messageSubject, messageText, frame))
            sendNotice.start()
        except:
            print("Error: unable to start image capture thread")
        print("image captured to /var/ramdisk")


def watchDogPetter():
    ''' 
    watchDogPetter is a true watchdog in the embedded sense.  The use of watchdogs in Linux operating systems
    takes on a different context and purpose.  In Linux severs the overriding purpose of the watchdog is to 
    prevent bricking of the server itself.  As a result the watchdog has been created so that critical processes
    such as network communication are monitored and the system is hard reset if those services cease to operate.
    Verifying operation via process ID is not effective for a multithreaded application, such as this one where 
    individual threads within the application need to be monitored.  As a result the hardware watchdog is called 
    through the watchdog driver.  Via an os call.  This is the same hardware which is called through the watchdog
    deamon (very unfortunately named watchdog as there is namespace colissions between the configuration of the 
    deamon and the driver, for which I have not found a definitive reference).  This implimentation does not 
    invoke the watchdog deamon as the hardware does not support competeing resources controlling the hardware.
    WARNING:  Be very careful to understand any changes made to the configuration of the driver and accidentally
    invoking the watchdog deamon as this will prevent this thread from operating.

    Globals:
        SUDO_PASSWORD (Constant String): sudo password for pi.
        WATCH_DOG_PET_INTERVAL (Constant Int): Time between petting the dog.
        keepAlive (Int): Timer counter indicating the timer thread is operating

    Returns:
        Nothing
    '''
    global SUDO_PASSWORD
    global WATCH_DOG_PET_INTERVAL
    global keepAlive

    command = 'sh -c "echo \'.\' >> /dev/watchdog"'
    lastKeepAlive = -1
    bufferedKeepAlive = -1
    petCounter = 1

    while True:
        if keepAlive != lastKeepAlive:
            p = os.system('echo %s|sudo -S %s' % (SUDO_PASSWORD, command))
            petCounter += 1
            with open(RAM_DISK + 'last_pet_time.txt', "w") as petFile:
                currentTime = datetime.datetime.now()
                fileText = f'Last pet at : {currentTime.strftime("%x %I:%M:%S %p")}'
                petFile.write(fileText)
        if petCounter % int(3 * NIGHT_MODE_FRAME_SLOWDOWN/WATCH_DOG_PET_INTERVAL) == 0:
            lastKeepAlive     = bufferedKeepAlive
            bufferedKeepAlive = keepAlive
        time.sleep(WATCH_DOG_PET_INTERVAL)

# Camera Init
camera            = PiCamera()
camera.resolution = (IM_WIDTH, IM_HEIGHT)
camera.framerate  = 30  # Was 10 originally
rawCapture        = PiRGBArray(camera, size=(IM_WIDTH, IM_HEIGHT))
rawCapture.truncate(0)

phase                   = 0
frameTotal              = 0
avgTime                 = 0
phaseFrame              = {}
phaseThread             = {}
referenceFrame          = None
(refX, refY,refW, refH) = (0, 0, 0, 0)
referenceFrameTime      = 0
lastActiveTime          = 0
dogImageCount           = 0
catImageCount           = 0
nightMode               = False
keepAlive               = 0

if WATCH_DOG_ENABLE and piHost:
    try:
        watchDogPetterThread = Thread(target=watchDogPetter)
        watchDogPetterThread.daemon = True
        watchDogPetterThread.start()
        print("Watch Dog Petting Thread: ", watchDogPetterThread)
    except:
        print("Error: unable to start Watch Dog Petting thread")

t1 = cv2.getTickCount()

# Continuously capture frames and use delays to implement DLL
for frame1 in camera.capture_continuous(rawCapture, format="bgr", use_video_port=True):

    # Acquire frame and expand frame dimensions to have shape: [1, None, None, 3]
    # i.e. a single-column array, where each item in the column has the pixel RGB value

    phaseFrame[phase] = np.copy(frame1.array)
    phaseFrame[phase].setflags(write=1)

    # Dark frames are detected to determine when to slow down the system as once the 
    # images are dark enough, the object detection will not work.  This allows the 
    # system to reduce power consumption and should extend the life of the Pi versus
    # running it hot 100% of the time.
    darkDetectFrame = imutils.resize(phaseFrame[phase], width=int(IM_WIDTH / 32))
    darkDetectFrame = cv2.cvtColor(darkDetectFrame, cv2.COLOR_BGR2GRAY)
    LumaAvg = cv2.mean(darkDetectFrame)
    if LumaAvg[0] < NIGHT_MODE_ON_THRESH and not nightMode:
        nightMode = True
    elif LumaAvg[0] > NIGHT_MODE_OFF_THRESH and nightMode:
        nightMode = False
        t1 = cv2.getTickCount()
        frameTotal = frameTotal % PHASES + PHASES
        avgTime = 0.5

    try:
        phaseThread[phase] = Thread(target=object_detector, args=(phaseFrame[phase], cvNet[phase]))
        phaseThread[phase].start()
    except:
         print(f"Error: unable to start thread {phase}")
    if frameTotal < PHASES:
        frame = np.copy(frame1.array)
        sleep(0.25)
    else:
        phaseThread[(phase + 1) % PHASES].join()
        frame = np.copy(phaseFrame[(phase + 1) % PHASES])
        deltaTime = (cv2.getTickCount() - t1) / freq
        if deltaTime < 0.75 * avgTime:
            sleep(0.9*(avgTime-deltaTime))
        if nightMode:
            sleep(NIGHT_MODE_FRAME_SLOWDOWN)

    # FPS calculation
    t2              = cv2.getTickCount()
    time1           = (t2 - t1) / freq
    avgTime         = (avgTime*frameTotal + time1) / (frameTotal + 1)
    frame_rate_calc = 1 / time1
    text            = f"FPS {frame_rate_calc:.2f}  Avg Time: {avgTime:.2f}  Avg Luma {LumaAvg[0]:.0f}"
    if frameCount % 1000 == 8: print(text)
    print(text)
    t1 = t2

    # resize image
    width   = int(frame.shape[1] * scale_percent / 100)
    height  = int(frame.shape[0] * scale_percent / 100)
    dim     = (width, height)
    display = cv2.resize(frame, dim, interpolation=cv2.INTER_AREA)

    # Draw FPS
    location  = (20, 20)
    fontScale = 1 * IM_HEIGHT / 1200
    thickness = 1
    cv2.putText(display, text, location, font, fontScale, BLUE, thickness, cv2.LINE_AA)

    # Display Frame
    if not args.serviceMode and DISPLAY_ON:  # Don't display in service mode as the desktop is not accessible
        cv2.imshow('Object detector', display)

        # Press 'q' to quit 'c' to capture image
        keyValue = cv2.waitKey(1)
        if keyValue == ord('q'):
            break
        elif keyValue == ord('c'):
            imageCapture = True
        else:
            imageCapture = False

    rawCapture.truncate(0)
    phase       = (phase + 1) % PHASES
    frameTotal += 1
    keepAlive  += 1

camera.close()

cv2.destroyAllWindows()
