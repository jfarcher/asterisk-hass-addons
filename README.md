# Home Assistant Add-on: Asterisk

A phone system for your home, run by [Asterisk](https://github.com/asterisk/asterisk) inside Home Assistant, that you set up in plain language rather than Asterisk config files.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjfarcher%2Fasterisk-hass-addons)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

## What it does

Everything is set up from the add-on's web UI in the Home Assistant sidebar:

- **Phones.** Add a desk phone, a phone app, the Home Assistant SIP card on a tablet, or a SIP doorbell. Each gets a short number and a generated password, and **How to connect** shows exactly what to type into the device. The list shows which phones are online, and **Test ring** checks one.
- **Automatic setup for desk phones.** Yealink, Grandstream, Snom, Fanvil and Poly phones can download their settings from the add-on. Point a phone at the setup address, or hand it out with DHCP option 66. The phone shows up as waiting, and one click gives it a number.
- **Outside lines.** Connect a SIP provider (VoIP account) with the server, username and password they gave you. Choose which phones ring for incoming calls, and whether outside numbers are dialled directly or after a prefix such as 9.
- **Apps.** Turn on ARI (the Asterisk REST Interface) with one switch, give apps their own logins, and send a range of numbers to an app.
- **Advanced.** An Asterisk console, for when you need one.

Your own Asterisk files still work: anything in the `custom` folder overrides the add-on's defaults. The web UI warns you if one of your files stops it working.

## Works with Kiosk Intercom Gateway

This add-on is built to pair with [Kiosk Intercom Gateway](https://github.com/jfarcher/kiosk-intercom-gateway), which connects [Kiosk Satellite](https://github.com/jxlarrea/kiosk-satellite) wall tablets to your phones. Phones can call kiosks, kiosks can call phones, and dialling 800 makes an announcement on every kiosk.

Connecting the two takes two clicks:

1. In this add-on's web UI, open **Apps** and press **Connect** beside Kiosk Intercom Gateway. This turns on ARI, creates the gateway's login and sends numbers 800-899 to the kiosks.
2. In Kiosk Intercom Gateway (0.8.0 or later), press **Set up Asterisk for me**. It picks up the login by itself, and neither app's files need editing.

## Get started

1. Add this repository with the button above, then install **Asterisk**.
2. In the add-on's **Configuration**, set an **AMI Password** (and an **Auto Add Secret** if you keep **Auto Add Extensions** on). Then start the add-on.
3. Open **Asterisk** from the sidebar and add your first phone.

Full documentation, including every option, is in [DOCS.md](asterisk/DOCS.md) (also shown on the add-on's Documentation tab).

## Where this came from

This project started as a fork of [TECH7Fox/asterisk-hass-addons](https://github.com/TECH7Fox/asterisk-hass-addons), and it still stands on that work. The way Asterisk is built and packaged, the startup and certificate handling, auto-created extensions for Home Assistant people, the `custom` config override, the mailbox server and the CI that publishes images all come from TECH7Fox, felipecrs and the other contributors there. Thank you.

It is now developed separately, for two reasons:

- **It has changed too much to merge back.** The web UI, the generated configuration behind it, outside lines, ARI apps and automatic phone setup are new, and they reshape how the dial plan and the add-on's settings work. Offering them upstream would mean rewriting someone else's project.
- **It has a different focus.** It aims to make a home phone system simple for people who don't know Asterisk, and to work closely with Kiosk Intercom Gateway.

It installs as a separate add-on, with its own configuration folder, so it doesn't replace the original. It still works with TECH7Fox's [Asterisk integration](https://github.com/TECH7Fox/Asterisk-integration) and [SIP card](https://github.com/TECH7Fox/HA-SIP). The [SIP-HASS docs](https://tech7fox.github.io/sip-hass-docs/) cover those.

This project keeps the original MIT licence; see [LICENSE](LICENSE).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
