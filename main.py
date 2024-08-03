####################################################################################
# MIT License
#
# Copyright (c) 2022 Maurício C. P. Pessoa
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
####################################################################################

import rp2
import network
import ubinascii
import machine
import urequests as requests
import time
from secrets import secrets

# Set country to avoid possible errors
rp2.country('DE')

wlan = network.WLAN(network.STA_IF)
wlan.active(True)

led = machine.Pin('LED', machine.Pin.OUT)

# See the MAC address in the wireless chip OTP
mac = ubinascii.hexlify(network.WLAN().config('mac'),':').decode()
print('mac = ' + mac)

# Load login data from different file for safety reasons
ssid = secrets['ssid']
pw = secrets['pw']
botToken = secrets['botToken']
chatId = secrets['telegramDmUid']

# Messages
startupText = 'I am online for the first time! Bot started!'
reconnectText = 'I am back online! Bot reconnected!'
text = 'Doorbell activated!'

# Commands
logCommand = "/log"

# In memory log
log = ''

# Wifi connection status
wifiData = ''

# Telegram update id for offset
updateId = 0

# Flags
isStartup = True

# Time variables
startupTime = time.ticks_ms()
lastLogCheck = startupTime
logCheckInterval = 30000
logMaxSize = 10000

# Delays
loopDelay = 1
buttonDelay = 5

# Button pressed value
pressed = 1

# Telegram send message URL
sendURL = 'https://api.telegram.org/bot' + botToken + '/sendMessage'

# Telegram getUpdates URL
getURL = 'https://api.telegram.org/bot' + botToken + '/getUpdates'
    
# Send a telegram message to a given user id
def send_message (chatId, message):
    param = {'chat_id': chatId, 'text': message}
    response = requests.post(sendURL, json=param)
    # Close to avoid filling up the RAM.
    response.close()

def read_message(chatId):
    global updateId
    url = ''
    if (updateId != 0):
        url = getURL + "?offset=" + str(updateId) + "?chat_id=" + str(chatId)
    else:
        url = getURL + "?chat_id=" + str(chatId)
    print(url)
    response = requests.get(url)
    print(response.text)
    json = response.json()
    for result in json['result']:
        updateId = result['update_id'] + 1
        print(result['channel_post']['text'])
        print(result['channel_post']['text'] == logCommand)
        if (result['channel_post']['text'] == logCommand):
            print_log(chatId)
    response.close()

def append_to_log(message):
    global log
    if (len(log) <= logMaxSize):
        log += str(time.ticks_ms()) + ' ' + message + '\n'
    else:
        print('Log is full. Not appending message.')

def print_log(chatId):
    global log
    print(log)
    send_message(chatId, log)
    if (len(log) > logMaxSize):
        reset_log()

def reset_log():
    global log, wifiData
    log = str(time.ticks_ms()) + ' ' + wifiData + '\n'

# Define blinking function for onboard LED to indicate error codes    
def blink_onboard_led(num_blinks):
    for i in range(num_blinks):
        led.on()
        time.sleep(.2)
        led.off()
        time.sleep(.2)
        
def is_wifi_connected():
    wlan_status = wlan.status()
    if wlan_status != 3:
        return False
    else:
        return True

def connect_wifi():
    global wifiData, isStartup
    while True:
        if (is_wifi_connected()):
            blink_onboard_led(3)
            led.on()
            status = wlan.ifconfig()
            print('ip = ' + status[0])
            wifiData = 'WiFi connected. IP: ' + status[0]
            append_to_log(wifiData)
            if (isStartup):
                send_message(chatId, startupText)
                isStartup = False
            else:
                send_message(chatId, reconnectText)
            break
        else:
            message = 'WiFi is disconnected. Trying to connect.'
            append_to_log(message)
            print(message)
            led.off()
            wlan.connect(ssid, pw)
            time.sleep(3)

# Connect to WiFi
connect_wifi()

# Setup GPIO pins
doorBellInput = machine.Pin(16, machine.Pin.IN, machine.Pin.PULL_DOWN)

while True:
    try:
        if (not is_wifi_connected()):
            connect_wifi()
        
        if (doorBellInput.value() == pressed):
            print('Doorbell pressed!')
            send_message(chatId, text)
            time.sleep(buttonDelay)
        
        # Check for new messages
        if (time.ticks_diff(time.ticks_ms(), lastLogCheck) > logCheckInterval):
            print('Checking for new messages...')
            read_message(chatId)
            lastLogCheck = time.ticks_ms()
        
        time.sleep(loopDelay)
        
    
    except KeyboardInterrupt:
        print('KeyboardInterrupt')
        break
    except Exception as e:
        print(e)
        led.off()
        wlan.disconnect()
        append_to_log('WiFi disconnected: ' + str(e))
        print(log)
        # Grace period.
        time.sleep(10)
        led.on()
        pass