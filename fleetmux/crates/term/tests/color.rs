//! Colour depth and the RGB → 256 downgrade (brief-m03 §3.4; package B13 `color-256`, M1). Red until M1.

use fleetmux_term::Color;
use fleetmux_term::color::{self, Depth};

#[test]
#[ignore = "M1: color-256"]
fn rgb_downgrades_to_nearest_cube_or_grey() {
    // Values fixed by brief-m03 §3.4.
    assert_eq!(color::to_256(Color::Rgb(255, 128, 0)), Color::Indexed(214));
    assert_eq!(
        color::to_256(Color::Rgb(128, 128, 128)),
        Color::Indexed(244)
    );
    assert_eq!(color::to_256(Color::Indexed(214)), Color::Indexed(214));
    assert_eq!(color::to_256(Color::Indexed(3)), Color::Indexed(3));
    assert_eq!(color::to_256(Color::Default), Color::Default);
}

#[test]
#[ignore = "M1: color-256"]
fn depth_from_colorterm_truecolor_or_24bit_else_256() {
    assert_eq!(
        color::depth_from_env("xterm-256color", "truecolor"),
        Depth::Rgb
    );
    assert_eq!(color::depth_from_env("xterm-256color", "24bit"), Depth::Rgb);
    assert_eq!(
        color::depth_from_env("xterm-256color", ""),
        Depth::Indexed256
    );
    assert_eq!(color::depth_from_env("screen", "yes"), Depth::Indexed256);
    assert_eq!(color::depth_from_env("", ""), Depth::Indexed256);
}
