# Home Assistant Add-on: Asterisk

Asterisk is a free and open source framework for building communications applications.
Asterisk powers IP PBX systems, VoIP gateways, conference servers, and is used by SMBs, enterprises, call centers, carriers and governments worldwide.

## Installation

Follow these steps to get the add-on installed on your system:

1. Click here:

   [![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjfarcher%2Fasterisk-hass-addons)

1. Scroll down the page to find the new repository, and click in the new add-on named **_Asterisk_**.
1. Click in the **_INSTALL_** button.

Or you can also use it as a standalone docker container. See [4.0.0 release notes](./CHANGELOG.md#400) for more information.

## Using

1. The certificate to your registered domain should already be created via the [Duck DNS](https://github.com/home-assistant/hassio-addons/tree/master/duckdns) or [Let's Encrypt](https://github.com/home-assistant/hassio-addons/tree/master/letsencrypt) add-on or another method. Make sure that the certificate files exist in the `/ssl/` directory.
2. Check the add-on configuration by going to the **_Configuration_** tab. You need to at least fill the _AMI Password_ and the _Auto add secret_ (if you leave _Auto add extensions_ enabled).
3. Start the add-on by clicking in the **_START_** button.

## Web UI

Open the add-on's web UI (**_OPEN WEB UI_**, or the sidebar panel) to set up your phone system without editing Asterisk files.

- **Phones**: add desk phones, phone apps, Home Assistant SIP cards/tablets and doorbells. Each gets a short number (starting at 200) and a generated password. **How to connect** shows exactly what to type into the device, and the list shows whether each one is online. Extensions created for Home Assistant people by the [`auto_add`](#option-auto_add) option are listed too, but are still managed by that option.
- **Automatic setup for desk phones** (bottom of the Phones tab): Yealink, Grandstream, Snom, Fanvil and Poly (Polycom) phones can download their settings from the add-on, so you don't have to type them in.
  1. Turn it on and copy the **Setup address**, such as `http://192.168.1.10:8097/p/<secret>/`.
  2. Enter that address as the provisioning or auto-provision server in the phone's web page, and restart the phone. If your router lets you set DHCP option 66, set it there instead and factory-reset phones find it by themselves.
  3. The phone appears under **Phones waiting to be set up**. Press **Set up** and give it a name.
  4. Restart the phone again, or press **Send settings now** once it's online.

  Phones are given Home Assistant's LAN IP address, found automatically. If that's the wrong one (for example with several networks), change it under **Address phones connect to**.

  To have phones download their settings through a reverse proxy such as NGINX, use **Phones reach Home Assistant through a reverse proxy?** under the setup address. It shows the `location /p/` block to add to the proxy. Home Assistant itself can't serve these files, because everything it serves needs a login.

  You can also type a phone's MAC address under **More options** when adding or editing it. Settings are served on port 8097 (the [`provisioning_port`](#option-provisioning_port) option) to phones that know the secret address, and only for MAC addresses you've set up. They travel unencrypted on your network.
- **Outside lines**: connect a phone service provider (SIP trunk / VoIP account) with the server, username and password they gave you. Choose which phones ring for incoming calls, and how outside numbers are dialled: either just dial the number (anything with 5 or more digits, or starting with `+`), or dial a prefix such as `9` first.
- **Apps**: let other Home Assistant apps answer, place and connect calls through ARI (the Asterisk REST Interface). Turn ARI on with **Allow apps to control calls**, and add a login for each app. You can also send a range of numbers to an app, such as 800-899. Apps we know about, like [Kiosk Intercom Gateway](https://github.com/jfarcher/kiosk-intercom-gateway), connect with one click: press **Connect**, then **Set up Asterisk for me** in the gateway. The status shows **Connected** once the app is signed in.
- **Advanced**: an Asterisk console and a button to reload the configuration.

Settings from the web UI are stored in `/addon_configs/aaf52c24_asterisk/asterisk/ui/pbx.json` and written to `pjsip_ui.conf`, `extensions_ui.conf` and `ari_ui.conf`, which are included by the default `pjsip.conf`, `extensions.conf` and `ari.conf`. Numbers sent to apps are routed from the `ui-apps` context and calls to outside lines from `ui-outbound`. The default `[default]` context includes both, in that order, before `local-extensions`.

If you replace any of those files with your own in the `custom` folder, it needs the same `#include` and `include =>` lines to keep the web UI working. The web UI shows a warning listing what's missing. This includes an `extensions.conf` written by an older Kiosk Intercom Gateway's setup button.

For each app, the web UI writes its login to `asterisk/integrations/<app>.json` in the add-on's config folder. Apps that can see other add-ons' config folders, such as Kiosk Intercom Gateway, read it from there, so nobody has to copy passwords around. ARI is served on port 8088 and needs the app's login.

The web UI is only reachable through Home Assistant. Note that Home Assistant lets non-admin users open it too, so anyone with a Home Assistant login can change phones and lines.

## Configuring Asterisk

The add-on copies all the default Asterisk config files to `/addon_configs/aaf52c24_asterisk/asterisk/default` on the add-on startup for your reference, and reads all customized config files from `/addon_configs/aaf52c24_asterisk/asterisk/custom`.

For example, if you need to change something in the `/etc/asterisk/extensions.conf`, you can copy the reference `/addon_configs/aaf52c24_asterisk/asterisk/default/extensions.conf` to `/addon_configs/aaf52c24_asterisk/asterisk/custom/extensions.conf` and make your changes there.

The way how this works is through symbolic links: if there is a custom config file, a symbolic link is created on top of the default config file pointing to the custom config file. Have in mind that this is only applicable for files under the root of `/addon_configs/aaf52c24_asterisk/asterisk/custom`. For example, the `/addon_configs/aaf52c24_asterisk/asterisk/custom/subdir/file.conf` **will not** be linked to `/etc/asterisk/subdir/file.conf`. The same applies to files that starts with a dot, like `.gitignore`.

**Note**: _Remember to restart the add-on when the Asterisk configuration files are changed._

## Configuring the add-on

We expose some configuration options to simplify the setup of the Asterisk server inside of the add-on. See below for more information on each option we provide.

**Note**: _Remember to restart the add-on when the configuration is changed._

### Option: `ami_password`

Set's the password for the Asterisk Manager Interface, to connect to the [Asterisk integration](https://github.com/TECH7Fox/Asterisk-integration).

### Option: `auto_add`

Creates a extension for every [person](https://www.home-assistant.io/integrations/person/) registered in Home Assistant. They will have their number and username auto-generated starting from 100, with the `callerid` set to the person's name.

**This is enabled by default for add-on users but disabled by default for container users.**

### Option: `auto_add_secret`

The secret for the auto generated extensions, when `auto_add` is enabled.

### Option: `video_support`

Enables video support for the auto generated extensions, when `auto_add` is enabled.

### Option: `additional_sounds`

The additional sounds languages to download from <https://downloads.asterisk.org/pub/telephony/sounds> on add-on startup, skipping when already downloaded. Examples: `fr`, `en_GB`.

The sounds will be downloaded to `/media/asterisk`.

If you want the add-on to re-download the sounds, you can simply remove the folder from `/media/asterisk` and restart it.

### Option: `register_ingress_entry`

Whether to register the ingress entry for the add-on as a Home Assistant entity or not. This is useful for the SIP card to avoid needing to manually expose Asterisk's WebSockets interface.

### Option: `generate_ssl_cert`

Enables/disables the generation of a self-signed certificate for use with the SSL interfaces (WSS and TLS).

### Option: `certfile`

The certificate file to use for SSL in your `/ssl/` folder, when `generate_ssl_cert` is disabled. If an absolute path is provided, it will be used as-is.

### Option: `keyfile`

The key file to use for SSL in your `/ssl/` folder, when `generate_ssl_cert` is disabled. If an absolute path is provided, it will be used as-is.

### Option: `mailbox`

Enables the mailbox server to send voicemails to the Asterisk Mailbox integration.

### Option: `mailbox_port`

The port used by the mailbox server.

### Option: `mailbox_password`

The password for the mailbox server.

### Option: `mailbox_extension`

Which extension to get the voicemails from.

### Option: `mailbox_google_api_key`

The API Key for the speech-to-text used by Asterisk Mailbox.
You can get a key [here](https://cloud.google.com/speech-to-text). Google says it's free, but requires a billing account.

### Option: `provisioning_port`

The port desk phones download their settings from, when **Automatic setup for desk phones** is turned on in the web UI. The add-on uses the host's network, so change it if another app already uses 8097.

### Option: `log_level`

The log level to configure Asterisk to use. To know more about the existing presets, check [`logger.conf`](./rootfs/usr/share/tempio/logger.conf.gtpl).

## STDIN action

You can use the STDIN action to run any Asterisk CLI commands that you want. For example:

```yaml
action: hassio.addon_stdin
data:
  addon: aaf52c24_asterisk
  input: dialplan reload
```

**This is only possible when using as an add-on.**

## Startup script

If the add-on finds a script at `/addon_configs/aaf52c24_asterisk/asterisk/startup.sh` it will run it before starting Asterisk. You can use this to install custom packages, dependancies, etc.

## Configuring the [Asterisk integration](https://github.com/TECH7Fox/Asterisk-integration)

- **_Host_**: `localhost` (when running as an add-on)
- **_Port_**: `5038`
- **_Username_**: `admin`
- **_Password_**: whatever you set in the `ami_password` configuration

## Configuring the [SIP.js card](https://github.com/TECH7Fox/HA-SIP)

- **_Host_**: `localhost` (when running as an add-on)
- **_Port_**: `8089`
- **_Video_**: `false` _Video is not working at the moment, this will be fixed soon. For now you could use the camera entity instead._

And add a extension. To see which extension every person has, you can look at `/addon_configs/aaf52c24_asterisk/asterisk/default/sip_default.conf`.

## Wiki

For more information about the Asterisk integration and SIP card, visit the [SIP-HASS docs](https://tech7fox.github.io/sip-hass-docs/).

## About

Maintained by [Jon Archer](https://github.com/jfarcher). This add-on started as a fork of [TECH7Fox/asterisk-hass-addons](https://github.com/TECH7Fox/asterisk-hass-addons), and the Asterisk build, startup handling and much of this documentation come from TECH7Fox, felipecrs and the other contributors there. It's now developed separately, with a web UI focused on simplicity and on working with [Kiosk Intercom Gateway](https://github.com/jfarcher/kiosk-intercom-gateway).
