"""Original Herdeck score: 128 BPM, F minor, 64 beats / 30 seconds.

All instruments are synthesized here; no samples or third-party recordings.
Requires Python 3.12+, numpy and ffmpeg. Seeded noise makes the source repeatable.
The master has a deliberate eight-beat build, beat-32 drop and resolved outro.
"""
from pathlib import Path
import subprocess
import wave
import numpy as np

RATE = 44100
BPM = 128
BEAT = 60 / BPM
DURATION = 64 * BEAT
rng = np.random.default_rng(128)
mix = np.zeros((round((DURATION + 2) * RATE), 2), dtype=np.float64)


def add(sound, beat, gain=1.0, pan=0.0, echoes=False):
    start = round(beat * BEAT * RATE)
    stereo = np.column_stack((sound * np.sqrt((1-pan)/2), sound * np.sqrt((1+pan)/2))) * gain
    end = min(start + len(sound), len(mix))
    mix[start:end] += stereo[:end-start]
    if echoes:
        for offset, decay in [(0.75, 0.22), (1.5, 0.12), (2.25, 0.065)]:
            pos = start + round(offset * BEAT * RATE)
            end = min(pos + len(sound), len(mix))
            if end > pos:
                mix[pos:end] += stereo[:end-pos, ::-1] * decay


def time(seconds):
    return np.arange(round(seconds * RATE)) / RATE


def tone(midi, seconds, kind='pluck'):
    t = time(seconds)
    f = 440 * 2 ** ((midi-69)/12)
    attack = np.minimum(t / 0.006, 1)
    release = np.minimum((seconds-t) / 0.035, 1)
    if kind == 'bass':
        # Soft analog harmonics on top of a clean sub, shaped around the kick.
        s = np.sin(2*np.pi*f*t) + .3*np.sin(4*np.pi*f*t) + .17*np.sin(6*np.pi*f*t)
        env = attack * release * np.exp(-t*3)
    else:
        s = sum(np.sin(2*np.pi*f*k*t + .008*np.sin(2*np.pi*4*t)) * np.exp(-t*k*3.5) / k for k in range(1, 9))
        s += .25*np.sin(2*np.pi*f*1.003*t)*np.exp(-t*5)
        env = attack * release
    return np.tanh(s*1.4) * env


def kick():
    t = time(.42)
    frequency = 47 + 105*np.exp(-t*40)
    phase = 2*np.pi*np.cumsum(frequency)/RATE
    click = rng.normal(0,1,len(t))*np.exp(-t*450)*.15
    return np.tanh((np.sin(phase)*np.exp(-t*10)+click)*1.7) * np.minimum(t/.001,1)


def snare():
    t = time(.2)
    noise = rng.normal(0,1,len(t))
    noise = noise - np.convolve(noise,np.ones(12)/12,'same')
    body = np.sin(2*np.pi*185*t)*np.exp(-t*28)
    clap = sum(np.exp(-np.maximum(t-o,0)*75)*(t>=o) for o in [0,.009,.022])
    return noise*clap*.22 + body*.35


def hat(opened=False):
    t = time(.19 if opened else .055)
    n = rng.normal(0,1,len(t)); n = np.diff(n,prepend=0)
    metal = sum(np.sin(2*np.pi*f*t) for f in [4201,5791,7307])*.05
    return (n*.18+metal)*np.exp(-t*(23 if opened else 78))*np.minimum(t/.001,1)


# Four-on-the-floor foundation, backbeat clap, syncopated percussion.
for b in range(64):
    intro = b < 8; build = 24 <= b < 32; outro = b >= 60
    if (not intro or b % 2 == 0) and not (build and b >= 28) and not outro:
        add(kick(),b,.88 if b>=32 else .75)
    if b % 2 == 1 and b>=8 and not outro:
        add(snare(),b,.64 if not build else .45,pan=.04)
    if 8 <= b < 60:
        add(hat(True),b+.5,.37,pan=.27)
        add(hat(),b+.25,.17,pan=-.3)
        add(hat(),b+.75,.23,pan=.25)
    if b in [15,23,39,47,55]:
        for k in [0.25,0.5,0.75]: add(snare(),b+k,.16+.18*k,pan=-.15)

# Fm / Db / Ab / Eb, with a low rhythmic bass and a bright arpeggiated hook.
chords = [(41,[65,68,72,77]),(37,[61,65,68,73]),(44,[63,68,72,75]),(39,[63,67,70,75])]
pattern = [0,2,1,3,2,1,3,2]
for bar in range(16):
    root,notes = chords[(bar//2)%4]
    b=bar*4
    if bar < 15 and not (6 <= bar < 8):
        for offset in [.5,1.25,2.5,3.25]:
            add(tone(root,.24,'bass'),b+offset,.52 if bar>=8 else .38,pan=0)
    for step in range(8):
        gain = .08 if bar<2 else (.12 if bar<6 else (.10 if bar<8 else .18))
        if bar>=15: gain=.07
        note=notes[pattern[step]]
        add(tone(note,.34),b+step*.5,gain,pan=(-.4 if step%2 else .4),echoes=True)
    # Wide, slower chord stabs give the drop harmonic weight.
    if 8 <= bar < 15:
        chord=sum(tone(note-12,.7) for note in notes[:3])/3
        add(chord,b,.32,pan=-.12,echoes=True)
        add(chord,b+2.5,.2,pan=.12,echoes=True)

# Snare acceleration and a filtered rising noise sweep before the drop.
for k in range(16):
    add(snare(),28+k*.25,.1+k*.019,pan=0)
t=time(8*BEAT)
noise=rng.normal(0,1,len(t))
noise=np.diff(noise,prepend=0)
sweep=np.sin(2*np.pi*(160*t+210*t*t))*.16+noise*.09
add(sweep*(t/t[-1])**2,24,.5,pan=0)
impact_t=time(1.5)
impact=(rng.normal(0,1,len(impact_t))*.13+np.sin(2*np.pi*41*impact_t)*.2)*np.exp(-impact_t*4)
add(impact,32,.8,echoes=True)
for note in [53,56,60,65]: add(tone(note,1.8),60,.16,pan=(note-59)/14,echoes=True)

master=mix[:round(DURATION*RATE)]
master=np.tanh(master*1.15)
master[:441]*=np.linspace(0,1,441)[:,None]
fade=round(.75*RATE);master[-fade:]*=np.linspace(1,0,fade)[:,None]
master=master/max(np.max(np.abs(master)),.01)*.91
out=Path(__file__).resolve().parents[1]/'public'/'film'
out.mkdir(parents=True,exist_ok=True)
wav=out/'herdeck-score.wav'
with wave.open(str(wav),'wb') as f:
    f.setnchannels(2);f.setsampwidth(2);f.setframerate(RATE)
    f.writeframes((master*32767).astype('<i2').tobytes())
subprocess.run(['ffmpeg','-y','-hide_banner','-loglevel','error','-i',str(wav),'-af','loudnorm=I=-14:TP=-1.5:LRA=7','-codec:a','libmp3lame','-b:a','192k',str(out/'herdeck-score.mp3')],check=True)
wav.unlink()  # Keep intermediate PCM out of Astro's published public directory.
print(f'Original 128 BPM score written to {out}/herdeck-score.mp3 ({DURATION:.2f}s)')
