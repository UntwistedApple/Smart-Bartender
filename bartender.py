#!/usr/bin/env python
# -*- coding: iso-8859-1 -*-

import time
import sys
import RPi.GPIO as GPIO
import json
import traceback
import threading
import textwrap
import subprocess

import Adafruit_GPIO.SPI as SPI
import Adafruit_SSD1306

from PIL import Image
from PIL import ImageFont
from PIL import ImageDraw

from menu import MenuItem, Menu, Back, MenuContext, MenuDelegate
from drinks import drink_list, drink_options

GPIO.setmode(GPIO.BCM)

SCREEN_WIDTH = 128
SCREEN_HEIGHT = 64

LEFT_BTN_PIN = 13
LEFT_PIN_BOUNCE = 200

RIGHT_BTN_PIN = 5
RIGHT_PIN_BOUNCE = 200

NUMBER_NEOPIXELS = 45
NEOPIXEL_DATA_PIN = 26
NEOPIXEL_CLOCK_PIN = 6
NEOPIXEL_BRIGHTNESS = 64

FLOW_RATE = 60.0 / 500.0

# Raspberry Pi pin configuration:
RST = 14
# Note the following are only used with SPI:
DC = 15
SPI_PORT = 0
SPI_DEVICE = 0

# Fontsize and Font Type Settings
FONTSIZE = 15
FONTFILE = "InputSans-Regular.ttf"

# Wraps Text for better view on OLED screen. 13 is best for 128x64
WRAPPER = textwrap.TextWrapper(width=13)


class Bartender(MenuDelegate):
    def __init__(self):
        self.running = False

        # set the oled screen height
        self.screen_width = SCREEN_WIDTH
        self.screen_height = SCREEN_HEIGHT

        self.btn1Pin = LEFT_BTN_PIN
        self.btn2Pin = RIGHT_BTN_PIN

        # configure interrups for buttons
        GPIO.setup(self.btn1Pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(self.btn2Pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        # configure screen
        spi_bus = 0
        spi_device = 0

		# Load the display driver. Attention: 128_64 is the display size. Needed to be changed if different in your setup
        self.led = Adafruit_SSD1306.SSD1306_128_64(rst=RST, dc=DC, spi=SPI.SpiDev(SPI_PORT, SPI_DEVICE,
                                                                                  max_speed_hz=8000000))  # Change rows & cols values depending on your display dimensions.

        # Initialize library.
        self.led.begin()

        # Clear display.
        self.led.clear()
        self.led.display()

        # Create image buffer.
        # Make sure to create image with mode '1' for 1-bit color.
        self.image = Image.new('1', (self.screen_width, self.screen_height))

        # Load default font.
        # self.font = ImageFont.load_default()
        self.font = ImageFont.truetype(FONTFILE, FONTSIZE)

        # Create drawing object.
        self.draw = ImageDraw.Draw(self.image)

        # load the pump configuration from file
        self.pump_configuration = Bartender.readPumpConfiguration()
        for pump in self.pump_configuration.keys():
            GPIO.setup(self.pump_configuration[pump]["pin"], GPIO.OUT, initial=GPIO.HIGH)

        print("Done initializing")

    @staticmethod
    def readPumpConfiguration():
        return json.load(open('pump_config.json'))

    @staticmethod
    def writePumpConfiguration(configuration):
        with open("pump_config.json", "w") as jsonFile:
            json.dump(configuration, jsonFile)

    def startInterrupts(self):
        GPIO.add_event_detect(self.btn1Pin, GPIO.FALLING, callback=self.left_btn, bouncetime=LEFT_PIN_BOUNCE)
        GPIO.add_event_detect(self.btn2Pin, GPIO.FALLING, callback=self.right_btn, bouncetime=RIGHT_PIN_BOUNCE)

    def buildMenu(self, drink_list, drink_options):
        # create a new main menu
        m = Menu("Main Menu")

        # add drink options
        drink_opts = []
        for d in drink_list:
            drink_opts.append(MenuItem('drink', d["name"], {"ingredients": d["ingredients"]}))

        configuration_menu = Menu("Configure")

        # add pump configuration options
        pump_opts = []
        for p in sorted(self.pump_configuration.keys()):
            config = Menu(self.pump_configuration[p]["name"])
            # add fluid options for each pump
            for opt in drink_options:
                # star the selected option
                selected = "*" if opt["value"] == self.pump_configuration[p]["value"] else ""
                config.addOption(
                    MenuItem('pump_selection', opt["name"], {"key": p, "value": opt["value"], "name": opt["name"]}))
            # add a back button so the user can return without modifying
            config.addOption(Back("Back"))
            config.setParent(configuration_menu)
            pump_opts.append(config)

        # add pump menus to the configuration menu
        configuration_menu.addOptions(pump_opts)
        # add a back button to the configuration menu
        configuration_menu.addOption(Back("Back"))
        # adds an option that cleans all pumps to the configuration menu
        configuration_menu.addOption(MenuItem('clean', 'Clean'))
        # adds an option that shuts down the rpi
        configuration_menu.addOption(MenuItem('shutdown', 'Shutdown'))

        configuration_menu.setParent(m)

        m.addOptions(drink_opts)
        m.addOption(configuration_menu)

        # create a menu context
        self.menuContext = MenuContext(m, self)

    def filterDrinks(self, menu):
        """
		Removes any drinks that can't be handled by the pump configuration
		"""
        for i in menu.options:
            if (i.type == "drink"):
                i.visible = False
                ingredients = i.attributes["ingredients"]
                presentIng = 0
                for ing in ingredients.keys():
                    for p in self.pump_configuration.keys():
                        if (ing == self.pump_configuration[p]["value"]):
                            presentIng += 1
                if (presentIng == len(ingredients.keys())):
                    i.visible = True
            elif (i.type == "menu"):
                self.filterDrinks(i)

    def selectConfigurations(self, menu):
        """
		Adds a selection star to the pump configuration option
		"""
        for i in menu.options:
            if (i.type == "pump_selection"):
                key = i.attributes["key"]
                if (self.pump_configuration[key]["value"] == i.attributes["value"]):
                    i.name = "%s %s" % (i.attributes["name"], "*")
                else:
                    i.name = i.attributes["name"]
            elif (i.type == "menu"):
                self.selectConfigurations(i)

    def prepareForRender(self, menu):
        self.filterDrinks(menu)
        self.selectConfigurations(menu)
        return True

    def menuItemClicked(self, menuItem):
        if (menuItem.type == "drink"):
            self.makeDrink(menuItem.name, menuItem.attributes["ingredients"])
            return True
        elif (menuItem.type == "pump_selection"):
            self.pump_configuration[menuItem.attributes["key"]]["value"] = menuItem.attributes["value"]
            Bartender.writePumpConfiguration(self.pump_configuration)
            return True
        elif (menuItem.type == "clean"):
            self.clean()
            return True
        elif (menuItem.type == "shutdown"):
            self.shutdown()
            return True
        return False

    def clean(self):
        waitTime = 20
        pumpThreads = []

        # cancel any button presses while the drink is being made
        # self.stopInterrupts()
        self.running = True

        for pump in self.pump_configuration.keys():
            pump_t = threading.Thread(target=self.pour, args=(self.pump_configuration[pump]["pin"], waitTime))
            pumpThreads.append(pump_t)

        # start the pump threads
        for thread in pumpThreads:
            thread.start()

        # start the progress bar
        self.progressBar(waitTime)

        # wait for threads to finish
        for thread in pumpThreads:
            thread.join()

        # show the main menu
        self.menuContext.showMenu()

        # sleep for a couple seconds to make sure the interrupts don't get triggered
        time.sleep(2);

    def shutdown(self):
        shutdowntext = "Shutdown takes 10 seconds. Bye!"
        self.led.clear()
        self.draw.rectangle((0, 0, self.screen_width, self.screen_height), outline=0, fill=0)

        words_list = WRAPPER.wrap(text=shutdowntext)
        TextNew = ''
        for ii in words_list[:-1]:
            TextNew = TextNew + ii + "\n"
        TextNew += words_list[-1]
        self.draw.text((0, 10), str(TextNew), font=self.font, fill=255)
        self.led.image(self.image)
        self.led.display()
        time.sleep(5);

        # Clean shutdown device
        subprocess.Popen(['shutdown', '-h', 'now'])

    def displayMenuItem(self, menuItem):
        print(menuItem.name)
        self.led.clear()
        self.draw.rectangle((0, 0, self.screen_width, self.screen_height), outline=0, fill=0)

        words_list = WRAPPER.wrap(text=menuItem.name)
        MenuItemNew = ''
        for ii in words_list[:-1]:
            MenuItemNew = MenuItemNew + ii + "\n"
        MenuItemNew += words_list[-1]
        self.draw.text((0, 10), str(MenuItemNew), font=self.font, fill=255)
        self.led.image(self.image)
        self.led.display()

    def cycleLights(self):
        pass

    def lightsEndingSequence(self):
        pass

    def pour(self, pin, waitTime):
        GPIO.output(pin, GPIO.LOW)
        time.sleep(waitTime)
        GPIO.output(pin, GPIO.HIGH)

    def progressBar(self, waitTime):
        interval = waitTime / 100.0
        for x in range(1, 101):
            self.led.clear()
            self.draw.rectangle((0, 0, self.screen_width, self.screen_height), outline=0, fill=0)
            self.updateProgressBar(x, y=35)
            self.led.image(self.image)
            self.led.display()
            time.sleep(interval)

    def makeDrink(self, drink, ingredients):
        # cancel any button presses while the drink is being made
        self.stopInterrupts()
        self.running = True

        # Parse the drink ingredients and spawn threads for pumps
        maxTime = 0
        pumpThreads = []
        for ing in ingredients.keys():
            for pump in self.pump_configuration.keys():
                if ing == self.pump_configuration[pump]["value"]:
                    waitTime = ingredients[ing] * FLOW_RATE
                    if (waitTime > maxTime):
                        maxTime = waitTime
                    pump_t = threading.Thread(target=self.pour, args=(self.pump_configuration[pump]["pin"], waitTime))
                    pumpThreads.append(pump_t)

        # start the pump threads
        for thread in pumpThreads:
            thread.start()

        # start the progress bar
        self.progressBar(maxTime)

        # wait for threads to finish
        for thread in pumpThreads:
            thread.join()

        # show the main menu
        self.menuContext.showMenu()

        # sleep for a couple seconds to make sure the interrupts don't get triggered
        time.sleep(2)

        # reenable interrupts
        # self.startInterrupts()
        self.running = False

    def left_btn(self, ctx):
        print("LEFT_BTN pressed")
        if not self.running:
            self.running = True
            self.menuContext.advance()
            print("Finished processing button press")
            self.running = False

    def right_btn(self, ctx):
        print("RIGHT_BTN pressed")
        if not self.running:
            self.running = True
            self.menuContext.select()
            print("Finished processing button press")
            self.running = False
            print("Starting button timeout")

    def updateProgressBar(self, percent, x=15, y=15):
        height = 10
        width = self.screen_width - 2 * x
        for w in range(0, width):
            self.draw.point((w + x, y), fill=255)
            self.draw.point((w + x, y + height), fill=255)
        for h in range(0, height):
            self.draw.point((x, h + y), fill=255)
            self.draw.point((self.screen_width - x, h + y), fill=255)
            for p in range(0, percent):
                p_loc = int(p / 100.0 * width)
                self.draw.point((x + p_loc, h + y), fill=255)

    def run(self):
        self.startInterrupts()
        # main loop
        try:

            try:

                while True:
                    letter = input(">")
                    if letter == "l":
                        self.left_btn(False)
                    if letter == "r":
                        self.right_btn(False)

            except EOFError:
                while True:
                    time.sleep(0.1)
                    if self.running not in (True, False):
                        self.running -= 0.1
                        if self.running == 0:
                            self.running = False
                            print("Finished button timeout")

        except KeyboardInterrupt:
            GPIO.cleanup()  # clean up GPIO on CTRL+C exit
        GPIO.cleanup()  # clean up GPIO on normal exit

        traceback.print_exc()


bartender = Bartender()
bartender.buildMenu(drink_list, drink_options)
bartender.run()




