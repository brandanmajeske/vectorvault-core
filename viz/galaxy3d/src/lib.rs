//! 3D camera + projection core for the VectorVault Memory Galaxy.
//!
//! Compiled to wasm32-unknown-unknown as a plain cdylib — hand-rolled C-ABI
//! exports, no wasm-bindgen, zero imports, so the browser can instantiate the
//! raw bytes with `WebAssembly.instantiate(bytes, {})`.
//!
//! Protocol:
//!   init(n)              -> pointer to the star input buffer (n * 3 f32: x,y,z in [-1,1])
//!   out_ptr()            -> pointer to the projected output buffer (n * 5 f32:
//!                           sx, sy, k (size multiplier), fog (0..1), depth)
//!   orbit/pan/dolly/keys -> accumulate user input (inertial)
//!   fly_to / reset / set_spin / update(dt, w, h)
//!
//! The camera orbits a chase target: every mutable quantity moves toward its
//! target each frame, which is what makes the motion feel fluid.

#![allow(static_mut_refs)]

const PITCH_LIM: f32 = 1.45;

struct Cam {
    yaw: f32,
    pitch: f32,
    dist: f32,
    tdist: f32,
    tx: f32,
    ty: f32,
    tz: f32,
    ttx: f32,
    tty: f32,
    ttz: f32,
    yaw_v: f32,
    pitch_v: f32,
    spin: f32,
    fpx: f32, // focal length in px (from last update; used to scale pan)
}

static mut CAM: Cam = Cam {
    yaw: 0.7,
    pitch: 0.32,
    dist: 9.0, // intro: start far out and glide in
    tdist: 3.1,
    tx: 0.0,
    ty: 0.0,
    tz: 0.0,
    ttx: 0.0,
    tty: 0.0,
    ttz: 0.0,
    yaw_v: 0.0,
    pitch_v: 0.0,
    spin: 0.0,
    fpx: 800.0,
};

static mut STARS: Vec<f32> = Vec::new();
static mut OUT: Vec<f32> = Vec::new();
static mut N: usize = 0;

#[no_mangle]
pub unsafe extern "C" fn init(n: usize) -> *mut f32 {
    N = n;
    STARS = vec![0.0; n * 3];
    OUT = vec![0.0; n * 5];
    STARS.as_mut_ptr()
}

#[no_mangle]
pub unsafe extern "C" fn out_ptr() -> *const f32 {
    OUT.as_ptr()
}

const ORBIT_G: f32 = 0.0024; // rad per px — classic orbit-viewer feel
const FLICK_MAX: f32 = 0.045; // rad per 60fps-step cap (~2.7 rad/s) — throwable, never spins out

/// Direct 1:1 drive while dragging: the galaxy tracks the hand exactly, so it
/// can never accelerate away mid-drag. (The old pure-velocity model compounded
/// momentum across mouse events — fast spins unless you were very careful.)
#[no_mangle]
pub unsafe extern "C" fn orbit(dx: f32, dy: f32) {
    CAM.yaw += dx * ORBIT_G;
    CAM.pitch = (CAM.pitch + dy * ORBIT_G).clamp(-PITCH_LIM, PITCH_LIM);
    CAM.yaw_v = 0.0;
    CAM.pitch_v = 0.0;
}

/// Momentum on release: JS passes the drag's recent px/frame velocity and the
/// galaxy keeps gliding, damped — a capped throw, not a runaway.
#[no_mangle]
pub unsafe extern "C" fn flick(vx: f32, vy: f32) {
    CAM.yaw_v = (vx * ORBIT_G * 0.85).clamp(-FLICK_MAX, FLICK_MAX);
    CAM.pitch_v = (vy * ORBIT_G * 0.85).clamp(-FLICK_MAX, FLICK_MAX);
}

#[no_mangle]
pub unsafe extern "C" fn pan(dx: f32, dy: f32) {
    // screen px -> world units at target depth
    let wpp = CAM.dist / CAM.fpx;
    let (sy, cy) = CAM.yaw.sin_cos();
    let (sp, cp) = CAM.pitch.sin_cos();
    // camera basis (right, up) for an orbit camera looking at the target
    let right = (cy, 0.0, -sy);
    let up = (-sp * sy, cp, -sp * cy);
    CAM.ttx -= (right.0 * dx + up.0 * -dy) * wpp;
    CAM.tty -= (right.1 * dx + up.1 * -dy) * wpp;
    CAM.ttz -= (right.2 * dx + up.2 * -dy) * wpp;
}

#[no_mangle]
pub unsafe extern "C" fn dolly(f: f32) {
    CAM.tdist = (CAM.tdist * f).clamp(0.35, 14.0);
}

/// keys bitmask: 1 left | 2 right | 4 up | 8 down (orbit)
///               16 W | 32 S | 64 A | 128 D (pan)  256 Q (in) | 512 E (out)
#[no_mangle]
pub unsafe extern "C" fn keys(mask: u32, dt: f32) {
    let o = 210.0 * dt; // orbit px-equivalent per second (direct-drive orbit)
    let p = 260.0 * dt; // pan px-equivalent per second
    if mask & 1 != 0 {
        orbit(-o, 0.0);
    }
    if mask & 2 != 0 {
        orbit(o, 0.0);
    }
    if mask & 4 != 0 {
        orbit(0.0, -o);
    }
    if mask & 8 != 0 {
        orbit(0.0, o);
    }
    if mask & 16 != 0 {
        pan(0.0, p);
    }
    if mask & 32 != 0 {
        pan(0.0, -p);
    }
    if mask & 64 != 0 {
        pan(p, 0.0);
    }
    if mask & 128 != 0 {
        pan(-p, 0.0);
    }
    if mask & 256 != 0 {
        dolly(1.0 - 1.6 * dt);
    }
    if mask & 512 != 0 {
        dolly(1.0 + 1.6 * dt);
    }
}

#[no_mangle]
pub unsafe extern "C" fn fly_to(x: f32, y: f32, z: f32, d: f32) {
    CAM.ttx = x;
    CAM.tty = y;
    CAM.ttz = z;
    CAM.tdist = d.clamp(0.35, 14.0);
}

#[no_mangle]
pub unsafe extern "C" fn reset() {
    CAM.ttx = 0.0;
    CAM.tty = 0.0;
    CAM.ttz = 0.0;
    CAM.tdist = 3.1;
    CAM.yaw_v = 0.0;
    CAM.pitch_v = 0.0;
}

#[no_mangle]
pub unsafe extern "C" fn set_spin(s: f32) {
    CAM.spin = s;
}

#[no_mangle]
pub unsafe extern "C" fn update(dt: f32, w: f32, h: f32) {
    let c = &mut CAM;
    let dtn = (dt * 60.0).min(3.0); // normalize to 60fps steps, clamp long frames

    // inertia + damping
    c.yaw += c.yaw_v * dtn + c.spin * dt;
    c.pitch = (c.pitch + c.pitch_v * dtn).clamp(-PITCH_LIM, PITCH_LIM);
    let damp = 0.92_f32.powf(dtn); // long, satisfying glide after a flick
    c.yaw_v *= damp;
    c.pitch_v *= damp;

    // chase targets
    let chase = 1.0 - 0.88_f32.powf(dtn);
    c.dist += (c.tdist - c.dist) * chase;
    c.tx += (c.ttx - c.tx) * chase;
    c.ty += (c.tty - c.ty) * chase;
    c.tz += (c.ttz - c.tz) * chase;

    // eye position on the orbit sphere
    let (sy, cy) = c.yaw.sin_cos();
    let (sp, cp) = c.pitch.sin_cos();
    let ex = c.tx + c.dist * cp * sy;
    let ey = c.ty + c.dist * sp;
    let ez = c.tz + c.dist * cp * cy;

    // view basis (forward toward target, right, up)
    let fwd = norm3(c.tx - ex, c.ty - ey, c.tz - ez);
    let right = norm3(fwd.2, 0.0, -fwd.0); // cross(fwd, world-up) — valid while |pitch| < PI/2
    let up = cross(right, fwd);

    let f = 0.9 * h.max(1.0);
    c.fpx = f;
    let n = N;
    for i in 0..n {
        let px = STARS[i * 3] - ex;
        let py = STARS[i * 3 + 1] - ey;
        let pz = STARS[i * 3 + 2] - ez;
        let depth = px * fwd.0 + py * fwd.1 + pz * fwd.2;
        let o = i * 5;
        if depth < 0.06 {
            OUT[o + 2] = 0.0; // size 0 => renderer skips
            OUT[o + 3] = 0.0;
            OUT[o + 4] = depth;
            continue;
        }
        let x = px * right.0 + py * right.1 + pz * right.2;
        let y = px * up.0 + py * up.1 + pz * up.2;
        OUT[o] = w * 0.5 + x * f / depth;
        OUT[o + 1] = h * 0.5 - y * f / depth;
        OUT[o + 2] = (c.dist / depth).clamp(0.05, 6.0); // size multiplier, 1 at target depth
        OUT[o + 3] = (1.35 - depth / (c.dist * 1.9)).clamp(0.22, 1.0); // depth fog
        OUT[o + 4] = depth;
    }
}

fn norm3(x: f32, y: f32, z: f32) -> (f32, f32, f32) {
    let l = (x * x + y * y + z * z).sqrt().max(1e-6);
    (x / l, y / l, z / l)
}

fn cross(a: (f32, f32, f32), b: (f32, f32, f32)) -> (f32, f32, f32) {
    (
        a.1 * b.2 - a.2 * b.1,
        a.2 * b.0 - a.0 * b.2,
        a.0 * b.1 - a.1 * b.0,
    )
}
