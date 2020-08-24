{ pkgs ? import <nixpkgs> {}
}:
with builtins;
with pkgs;
writers.writePython3Bin "melpazoid" {
  libraries = [ pkgs.python3Packages.requests ];
  flakeIgnore = [ "E501" "W503" ];
} (readFile ./melpazoid/melpazoid.py)
