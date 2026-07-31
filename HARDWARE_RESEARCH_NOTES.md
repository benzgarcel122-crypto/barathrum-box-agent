# Hardware & Image-Build Research Notes (desk research only, no physical board)

Everything below is **desk research from public sources, not hardware-verified.** It exists to
save time once a real Orange Pi One is in hand — it does NOT substitute for STEP 0's actual
bench-testing requirement. Treat every claim here as "worth trying first," not "confirmed."

## 1. GPIO pinout — independently re-confirmed, still not resolved to exact numbers

The MPD already flags the Orange Pi One's 40-pin header as reportedly mirrored/flipped 180°
relative to Orange Pi PC / Raspberry Pi, from community documentation. This session independently
found the same claim from a second, separate source:

- GitHub wiki, "About Orange Pi One GPIO Pins": *"The Orange Pi One & Lite both have a Raspberry
  Pi model B+ compatible 40-pin, 0.1" connector... Warning: The header's orientation on these 2
  boards is 180°, please check 'pin 1' marking carefully."*
  (https://github.com/cagritrk/OrangePi.GPIO_Examples/wiki/About-Orange-Pi-One-GPIO-Pins)
- A 2016 Armbian community forum thread asks the identical question and gets the identical
  "flipped 180 from [Raspberry Pi]" answer, unresolved to a definitive diagram in that thread
  itself. (https://forum.armbian.com/topic/1615-orange-pi-one-gpio-pin-layout/)
- Pascal Roeleven maintains per-model Orange Pi GPIO pinout diagrams specifically because official
  documentation is inconsistent across models — worth checking his Orange Pi One page directly
  when wiring: https://pascalroeleven.nl/2020/04/13/orange-pi-gpio-pinouts/
- The original Xunlong (Orange Pi manufacturer) schematic PDF for the One is circulating publicly
  (e.g. via Scribd, "ORANGE_PI-ONE-V1_1") and contains the actual GPIO assignment table sheet —
  this is the authoritative source if a specific pin's function needs confirming, but it's a raw
  schematic, not a beginner pinout diagram.

**Net effect on this codebase:** no change — `config.DEFAULT_COIN_PIN = 7` and
`DEFAULT_RELAY_PIN = 11` remain unverified placeholders, exactly as already flagged. This research
only adds a second independent source confirming the flip warning is real and widely known, plus
two concrete diagram/schematic sources to check first when the physical board arrives, rather than
starting from zero.

## 2. Image-build tooling — this one IS resolved, concretely

STEP 1's MPD section lists "confirm the actual image-build tooling" as an open action item,
undecided between Armbian's own build framework vs. a simpler manual "customize a base image, dd
it" approach. Confirmed this session: **Orange Pi One is an officially-supported Armbian board**,
not just community-patched — armbian.com lists it directly with current stable images (Ubuntu
26.04 / Debian 13, kernel 6.18.33 as of this writing) and the exact reproducible build command:

```
git clone https://github.com/armbian/build.git
cd build
touch userpatches/customize-image.sh   # hook for baking in this repo's install steps
./compile.sh BOARD=orangepione RELEASE=trixie BUILD_DESKTOP=no BUILD_MINIMAL=yes KERNEL_CONFIGURE=no
```

`userpatches/customize-image.sh` is Armbian's standard, documented hook for exactly what STEP 1
needs baked into the image: installing this repo's dependencies, enabling the
`systemd/barathrum-agent.service` unit, and pre-writing `hostapd`/`dnsmasq` base config so the
image boots directly into working box-agent mode.

**Recommendation:** use Armbian's own build framework via `customize-image.sh`, not a manual
dd-based approach. It's the officially-supported path for this exact board (not a workaround), and
produces the `.img`/`.img.xz` format Balena Etcher requires directly, with no extra conversion
step. This is desk research, not a completed build — actually running `compile.sh` and producing a
working image is still real, unstarted work, likely best done alongside the first real bench-test
session rather than blind ahead of it (build failures are far easier to debug with the target
board on-hand to test against immediately).

## Sources
- https://github.com/cagritrk/OrangePi.GPIO_Examples/wiki/About-Orange-Pi-One-GPIO-Pins
- https://forum.armbian.com/topic/1615-orange-pi-one-gpio-pin-layout/
- https://pascalroeleven.nl/2020/04/13/orange-pi-gpio-pinouts/
- https://www.scribd.com/document/348062287/ORANGE-PI-ONE-V1-1
- https://armbian.com/boards/orangepione
- https://github.com/armbian/build
