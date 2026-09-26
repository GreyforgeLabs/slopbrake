"""Generates brand/logo.svg: a drilled steel brake rotor, a cyan hub lattice, one amber caliper."""
import math

CX = CY = 256


def pol(r, a):
    a = math.radians(a)
    return CX + r * math.cos(a), CY + r * math.sin(a)


def sector(r1, r2, a1, a2):
    p1, p2, p3, p4 = pol(r2, a1), pol(r2, a2), pol(r1, a2), pol(r1, a1)
    return (f"M{p1[0]:.1f},{p1[1]:.1f} A{r2},{r2} 0 0 1 {p2[0]:.1f},{p2[1]:.1f} "
            f"L{p3[0]:.1f},{p3[1]:.1f} A{r1},{r1} 0 0 0 {p4[0]:.1f},{p4[1]:.1f} Z")


def svg():
    holes = [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10.5" fill="url(#hole)" stroke="#05070a" stroke-width="1.5"/>'
             for x, y in (pol(160, i * 30 + 15) for i in range(12))]
    holes += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="url(#hole)" stroke="#05070a" stroke-width="1.5"/>'
              for x, y in (pol(132, i * 30) for i in range(12))]
    machining = "".join(f'<circle cx="256" cy="256" r="{r}" fill="none" stroke="#fff" stroke-opacity=".05" stroke-width="1"/>'
                        for r in range(118, 196, 6))
    hexo = " ".join(f"{pol(80, a)[0]:.1f},{pol(80, a)[1]:.1f}" for a in range(-90, 270, 60))
    hexi = " ".join(f"{pol(68, a)[0]:.1f},{pol(68, a)[1]:.1f}" for a in range(-90, 270, 60))
    nodes = [pol(44, a) for a in range(-90, 270, 60)]
    spokes = "".join(f'<line x1="{nodes[i][0]:.1f}" y1="{nodes[i][1]:.1f}" x2="{nodes[i + 3][0]:.1f}" y2="{nodes[i + 3][1]:.1f}"/>'
                     for i in range(3))
    node_dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="url(#sage)" stroke="#1b231b" stroke-width="2"/>'
                        for x, y in nodes)
    bolts = "".join(f'<circle cx="{pol(203, a)[0]:.1f}" cy="{pol(203, a)[1]:.1f}" r="7.5" fill="url(#bolt)" stroke="#5a3606" stroke-width="2"/>'
                    for a in (-70, -26))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512" role="img" aria-label="Slopbrake logo: a steel brake rotor gripped by an amber caliper">
<defs>
 <linearGradient id="steel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#c9d0d7"/><stop offset=".45" stop-color="#7d868f"/><stop offset=".55" stop-color="#8f98a1"/><stop offset="1" stop-color="#3b4249"/></linearGradient>
 <linearGradient id="steel2" x1="1" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#dfe4e9"/><stop offset=".5" stop-color="#8e979f"/><stop offset="1" stop-color="#474e56"/></linearGradient>
 <radialGradient id="band" cx=".5" cy=".5" r=".5"><stop offset=".55" stop-color="#000" stop-opacity="0"/><stop offset=".8" stop-color="#000" stop-opacity=".2"/><stop offset="1" stop-color="#000" stop-opacity=".06"/></radialGradient>
 <radialGradient id="hole" cx=".35" cy=".35" r=".8"><stop offset="0" stop-color="#1c2229"/><stop offset="1" stop-color="#07090c"/></radialGradient>
 <radialGradient id="sage" cx=".35" cy=".35" r=".8"><stop offset="0" stop-color="#c3d6ba"/><stop offset="1" stop-color="#6f8a68"/></radialGradient>
 <radialGradient id="bolt" cx=".35" cy=".35" r=".8"><stop offset="0" stop-color="#ffe2b0"/><stop offset="1" stop-color="#a86008"/></radialGradient>
 <linearGradient id="amber" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffc56b"/><stop offset=".5" stop-color="#fda52b"/><stop offset="1" stop-color="#b3650a"/></linearGradient>
 <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="10" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
 <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#000" flood-opacity=".5"/></filter>
 <filter id="cyanglow" x="-40%" y="-40%" width="180%" height="180%"><feGaussianBlur stdDeviation="2.4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<g filter="url(#shadow)">
 <circle cx="256" cy="256" r="204" fill="url(#steel)" stroke="#23282e" stroke-width="5"/>
 <circle cx="256" cy="256" r="204" fill="url(#band)"/>
 {machining}
 <circle cx="256" cy="256" r="194" fill="none" stroke="#fff" stroke-opacity=".16" stroke-width="2"/>
 <circle cx="256" cy="256" r="112" fill="none" stroke="#23282e" stroke-width="4"/>
 <circle cx="256" cy="256" r="108" fill="none" stroke="#fff" stroke-opacity=".12" stroke-width="1.5"/>
 {"".join(holes)}
 <polygon points="{hexo}" fill="url(#steel2)" stroke="#1d2126" stroke-width="4" stroke-linejoin="round"/>
 <polygon points="{hexi}" fill="#10151a" stroke="#38c8e8" stroke-opacity=".3" stroke-width="1.5" stroke-linejoin="round"/>
 <g fill="none" stroke="#38c8e8" stroke-width="2.6" stroke-linecap="round" filter="url(#cyanglow)">
  <circle cx="256" cy="256" r="44"/>{spokes}
 </g>
 {node_dots}
 <circle cx="256" cy="256" r="11" fill="#07090c" stroke="#38c8e8" stroke-opacity=".7" stroke-width="2"/>
</g>
<g filter="url(#glow)">
 <path d="{sector(164, 232, -84, -12)}" fill="url(#amber)" stroke="#6b3d05" stroke-width="3" stroke-linejoin="round"/>
</g>
<path d="{sector(164, 176, -82, -14)}" fill="#2a1a06" fill-opacity=".85"/>
<path d="{sector(218, 228, -80, -16)}" fill="#fff" fill-opacity=".3"/>
{bolts}
</svg>
'''


if __name__ == "__main__":
    with open("logo.svg", "w", encoding="utf-8") as handle:
        handle.write(svg())
