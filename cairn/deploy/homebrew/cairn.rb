# Homebrew formula for the `cairn` command line: the single-file binaries of a GitHub release.
#
# This file is a template: the release job (.github/workflows/cairn-cli-release.yml) fills in @@VERSION@@,
# @@REPOSITORY@@ and the four sha256 sums, and attaches the result to the release as cairn.rb. To publish it, copy
# that file into a tap (a repository named homebrew-<tap>, e.g. acme/homebrew-cairn, under Formula/cairn.rb):
#
#   brew tap acme/cairn            # https://github.com/acme/homebrew-cairn
#   brew install cairn
#
# The binaries are not signed until the release has signing secrets (see docs/site/command-line.md).
class Cairn < Formula
  desc "Command line for Cairn: sign in, then query, search, show and ask as yourself"
  homepage "https://github.com/@@REPOSITORY@@"
  version "@@VERSION@@"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/@@REPOSITORY@@/releases/download/cli-v@@VERSION@@/cairn-macos-arm64"
      sha256 "@@SHA256_MACOS_ARM64@@"
    end
    on_intel do
      url "https://github.com/@@REPOSITORY@@/releases/download/cli-v@@VERSION@@/cairn-macos-x86_64"
      sha256 "@@SHA256_MACOS_X86_64@@"
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/@@REPOSITORY@@/releases/download/cli-v@@VERSION@@/cairn-linux-arm64"
      sha256 "@@SHA256_LINUX_ARM64@@"
    end
    on_intel do
      url "https://github.com/@@REPOSITORY@@/releases/download/cli-v@@VERSION@@/cairn-linux-x86_64"
      sha256 "@@SHA256_LINUX_X86_64@@"
    end
  end

  def install
    bin.install Dir["cairn-*"].first => "cairn"
    # A bare binary is downloaded without its execute bit; it must run for its completions and for people.
    chmod 0755, bin/"cairn"
    generate_completions_from_executable(bin/"cairn", "completion")
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/cairn --version")
    assert_match "_cairn_complete", shell_output("#{bin}/cairn completion bash")
  end
end
