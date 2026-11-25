// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ComplianceRegistry} from "../src/ComplianceRegistry.sol";

contract ComplianceRegistryTest is Test {
    ComplianceRegistry internal registry;
    address internal officer = makeAddr("officer");
    address internal outsider = makeAddr("outsider");

    bytes32 internal constant REPORT = keccak256("report-1");
    bytes32 internal constant HASH = bytes32(uint256(0xA11CE));
    bytes32 internal constant OTHER_HASH = bytes32(uint256(0xBAD));
    bytes32 internal constant ROOT = bytes32(uint256(0xB0B));
    bytes32 internal constant EVENTS = bytes32(uint256(0xE7E));
    bytes32 internal constant RULES = bytes32(uint256(0x5EC));
    uint32 internal constant COUNT = 3;
    uint8 internal constant SEVERITY = 4;

    event ReportAnchored(
        bytes32 indexed reportId,
        uint64 indexed sequence,
        address indexed reporter,
        bytes32 reportHash,
        bytes32 alertsRoot,
        bytes32 chainHead,
        string uri
    );
    event ReporterAdded(address indexed account);
    event ReporterRemoved(address indexed account);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    function setUp() public {
        registry = new ComplianceRegistry();
        registry.addReporter(officer);
    }

    function _file(bytes32 id, bytes32 hash) internal returns (uint64) {
        vm.prank(officer);
        return registry.anchor(id, hash, ROOT, EVENTS, RULES, COUNT, SEVERITY, "https://example.org/report.json");
    }

    /// The running hash after the officer files (id, hash) with the usual evidence, now.
    function _head(bytes32 previous, bytes32 id, bytes32 hash) internal view returns (bytes32) {
        return sha256(
            abi.encodePacked(
                previous, id, hash, ROOT, EVENTS, RULES, COUNT, SEVERITY, officer, uint64(vm.getBlockTimestamp())
            )
        );
    }

    // -- filing ----------------------------------------------------------------------------------

    function test_AnchorStoresEverything() public {
        vm.warp(1_790_000_000);
        uint64 sequence = _file(REPORT, HASH);
        assertEq(sequence, 1);

        ComplianceRegistry.Anchor memory filed = registry.get(REPORT);
        assertEq(filed.reportHash, HASH);
        assertEq(filed.alertsRoot, ROOT);
        assertEq(filed.eventsHash, EVENTS);
        assertEq(filed.rulebookHash, RULES);
        assertEq(filed.chainHead, _head(bytes32(0), REPORT, HASH));
        assertEq(filed.anchoredAt, 1_790_000_000);
        assertEq(filed.sequence, 1);
        assertEq(filed.alertCount, COUNT);
        assertEq(filed.worstSeverity, SEVERITY);
        assertEq(filed.reporter, officer);
        assertEq(registry.count(), 1);
        assertEq(registry.reportIdAt(1), REPORT);
        assertTrue(registry.isAnchored(REPORT));
    }

    function test_AnchorEmitsEvent() public {
        vm.expectEmit(true, true, true, true);
        emit ReportAnchored(
            REPORT, 1, officer, HASH, ROOT, _head(bytes32(0), REPORT, HASH), "https://example.org/report.json"
        );
        _file(REPORT, HASH);
    }

    function test_ReportCannotBeReplaced() public {
        _file(REPORT, HASH);
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.AlreadyAnchored.selector, REPORT));
        _file(REPORT, OTHER_HASH);
        // Not even with the same fingerprint: one identifier, one filing.
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.AlreadyAnchored.selector, REPORT));
        _file(REPORT, HASH);
        assertEq(registry.get(REPORT).reportHash, HASH);
        assertEq(registry.count(), 1);
    }

    function test_AFilingInABlockWithTimestampZeroIsStillWriteOnce() public {
        // Never true of a public network, but a development chain can be started that way, and
        // "was this identifier used?" must not hang on the clock.
        vm.warp(0);
        _file(REPORT, HASH);
        assertTrue(registry.isAnchored(REPORT));
        assertTrue(registry.verifyReport(REPORT, HASH));
        assertEq(registry.get(REPORT).anchoredAt, 0);

        vm.warp(1_790_000_000);
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.AlreadyAnchored.selector, REPORT));
        _file(REPORT, OTHER_HASH);
        assertEq(registry.get(REPORT).reportHash, HASH);
        assertEq(registry.count(), 1);
    }

    function testFuzz_AFiledEntryNeverChanges(bytes32[6] memory ids, bytes32[6] memory hashes, uint32 elapsed) public {
        vm.warp(1_790_000_000);
        _file(REPORT, HASH);
        bytes32 before = keccak256(abi.encode(registry.get(REPORT)));

        // Whatever is filed afterwards, by whoever, and however the roles change.
        for (uint256 i = 0; i < ids.length; ++i) {
            vm.warp(vm.getBlockTimestamp() + elapsed);
            vm.prank(officer);
            try registry.anchor(ids[i], hashes[i], hashes[i], EVENTS, RULES, uint32(i), uint8(i % 6), "") {} catch {}
        }
        registry.removeReporter(officer);
        registry.transferOwnership(outsider);

        assertEq(keccak256(abi.encode(registry.get(REPORT))), before);
        assertEq(registry.reportIdAt(1), REPORT);
        assertTrue(registry.verifyReport(REPORT, HASH));
    }

    function test_OnlyReportersMayFile() public {
        vm.prank(outsider);
        vm.expectRevert(ComplianceRegistry.NotReporter.selector);
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 0, "");
    }

    function test_RevokedReporterMayNotFile() public {
        registry.removeReporter(officer);
        vm.prank(officer);
        vm.expectRevert(ComplianceRegistry.NotReporter.selector);
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 0, "");
    }

    function test_RejectsEmptyFingerprintAndBadSeverity() public {
        vm.startPrank(officer);
        vm.expectRevert(ComplianceRegistry.EmptyFingerprint.selector);
        registry.anchor(REPORT, bytes32(0), ROOT, EVENTS, RULES, 0, 0, "");
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.InvalidSeverity.selector, uint8(6)));
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 6, "");
        // The most serious level there is, is a level.
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 5, "");
        vm.stopPrank();
        assertEq(registry.get(REPORT).worstSeverity, 5);
    }

    function test_GetRevertsForUnknownReport() public {
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.UnknownReport.selector, REPORT));
        registry.get(REPORT);
    }

    function test_ReportIdAtIsBoundsChecked() public {
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.NoSuchEntry.selector, 0));
        registry.reportIdAt(0);
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.NoSuchEntry.selector, 1));
        registry.reportIdAt(1);
        _file(REPORT, HASH);
        assertEq(registry.reportIdAt(1), REPORT);
        vm.expectRevert(abi.encodeWithSelector(ComplianceRegistry.NoSuchEntry.selector, 2));
        registry.reportIdAt(2);
    }

    // -- the chain -------------------------------------------------------------------------------

    function test_HeadChainsEveryAnchor() public {
        assertEq(registry.head(), bytes32(0));
        bytes32 expected = bytes32(0);
        for (uint256 i = 1; i <= 5; ++i) {
            vm.warp(1_790_000_000 + i * 60);
            bytes32 id = keccak256(abi.encode("report", i));
            bytes32 hash = keccak256(abi.encode("hash", i));
            _file(id, hash);
            expected = _head(expected, id, hash);
            assertEq(registry.head(), expected);
            assertEq(registry.get(id).chainHead, expected);
            assertEq(registry.get(id).sequence, i);
        }
        assertEq(registry.count(), 5);
    }

    /// File one report in a fresh registry and return the head.
    function _headAfter(address reporter, uint256 time, bytes32 hash, uint32 alertCount, uint8 severity)
        internal
        returns (bytes32)
    {
        ComplianceRegistry fresh = new ComplianceRegistry();
        fresh.addReporter(reporter);
        vm.warp(time);
        vm.prank(reporter);
        fresh.anchor(REPORT, hash, ROOT, EVENTS, RULES, alertCount, severity, "");
        return fresh.head();
    }

    function test_HeadCommitsToEveryFieldOfAnEntry() public {
        bytes32 base = _headAfter(officer, 1_000, HASH, 4, 5);
        assertEq(_headAfter(officer, 1_000, HASH, 4, 5), base, "the same filing gives the same head");
        assertTrue(_headAfter(officer, 1_000, OTHER_HASH, 4, 5) != base, "report hash");
        // The count is what stops an interior node passing as an alert, so it must be pinned too.
        assertTrue(_headAfter(officer, 1_000, HASH, 2, 5) != base, "alert count");
        assertTrue(_headAfter(officer, 1_000, HASH, 4, 0) != base, "severity");
        assertTrue(_headAfter(outsider, 1_000, HASH, 4, 5) != base, "reporter");
        assertTrue(_headAfter(officer, 2_000, HASH, 4, 5) != base, "time");
    }

    function testFuzz_AnchorsAreIndependent(bytes32 idA, bytes32 idB, bytes32 hashA, bytes32 hashB) public {
        vm.assume(idA != idB && hashA != bytes32(0) && hashB != bytes32(0));
        _file(idA, hashA);
        _file(idB, hashB);
        assertTrue(registry.verifyReport(idA, hashA));
        assertTrue(registry.verifyReport(idB, hashB));
        assertEq(registry.verifyReport(idA, hashB), hashA == hashB);
    }

    // -- looking up reports by the evidence they are about -----------------------------------------

    function test_EveryReportAboutAnEventStreamCanBeFound() public {
        bytes32 otherEvents = bytes32(uint256(0x0E0E));
        assertEq(registry.reportsAbout(EVENTS).length, 0);

        bytes32 convenient = keccak256("report-1 "); // a second report on the same match, under another name
        bytes32 unrelated = keccak256("report-2");
        vm.startPrank(officer);
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 7, 5, "");
        registry.anchor(unrelated, HASH, ROOT, otherEvents, RULES, 7, 5, "");
        registry.anchor(convenient, OTHER_HASH, ROOT, EVENTS, RULES, 0, 0, "");
        vm.stopPrank();

        bytes32[] memory about = registry.reportsAbout(EVENTS);
        assertEq(about.length, 2);
        assertEq(about[0], REPORT);
        assertEq(about[1], convenient);
        assertEq(registry.reportsAbout(otherEvents).length, 1);
        assertEq(registry.reportsAbout(otherEvents)[0], unrelated);
        assertEq(registry.reportsAbout(bytes32(0)).length, 0);
    }

    // -- verification ----------------------------------------------------------------------------

    function test_VerifyReport() public {
        assertFalse(registry.verifyReport(REPORT, HASH));
        assertFalse(registry.verifyReport(REPORT, bytes32(0)), "an unused identifier verifies nothing");
        _file(REPORT, HASH);
        assertTrue(registry.verifyReport(REPORT, HASH));
        assertFalse(registry.verifyReport(REPORT, OTHER_HASH));
        assertFalse(registry.verifyReport(keccak256("other"), HASH));
    }

    function test_LeafHashMatchesRfc6962() public view {
        // The test vector of RFC 6962: the leaf hash of the empty string.
        assertEq(registry.leafHash(""), 0x6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d);
    }

    /// The reference tree below is this test's own code, so pin it, and the contract with it, to
    /// answers from elsewhere: the Merkle tree test vectors of the Certificate Transparency project.
    function test_AgreesWithTheCertificateTransparencyTestVectors() public view {
        bytes[] memory inputs = new bytes[](8);
        inputs[0] = hex"";
        inputs[1] = hex"00";
        inputs[2] = hex"10";
        inputs[3] = hex"2021";
        inputs[4] = hex"3031";
        inputs[5] = hex"40414243";
        inputs[6] = hex"5051525354555657";
        inputs[7] = hex"606162636465666768696a6b6c6d6e6f";
        bytes32[8] memory roots = [
            bytes32(0x6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d),
            0xfac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125,
            0xaeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77,
            0xd37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7,
            0x4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4,
            0x76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef,
            0xddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c,
            0x5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328
        ];
        bytes32[] memory leaves = new bytes32[](8);
        for (uint256 i = 0; i < 8; ++i) {
            leaves[i] = registry.leafHash(inputs[i]);
        }
        for (uint256 size = 1; size <= 8; ++size) {
            assertEq(_root(leaves, 0, size), roots[size - 1], "root");
            for (uint256 index = 0; index < size; ++index) {
                bytes32[] memory proof = _proof(leaves, index, 0, size);
                assertTrue(registry.verifyInclusion(leaves[index], index, size, proof, roots[size - 1]), "path");
            }
        }
    }

    function test_VerifyAlertAgainstAnchoredRoot() public {
        bytes32[] memory leaves = _leaves(7, 42);
        bytes32 root = _root(leaves, 0, 7);
        vm.prank(officer);
        registry.anchor(REPORT, HASH, root, EVENTS, RULES, 7, 3, "");

        for (uint256 i = 0; i < 7; ++i) {
            bytes32[] memory proof = _proof(leaves, i, 0, 7);
            assertTrue(registry.verifyAlert(REPORT, leaves[i], i, proof));
            assertFalse(registry.verifyAlert(REPORT, leaves[i], (i + 1) % 7, proof));
            assertFalse(registry.verifyAlert(REPORT, keccak256("forged"), i, proof));
        }
        assertFalse(registry.verifyAlert(keccak256("other"), leaves[0], 0, _proof(leaves, 0, 0, 7)));
    }

    function test_VerifyAlertWhenAReportHasNoAlertsOrOne() public {
        bytes32[] memory none = new bytes32[](0);
        bytes32[] memory one = new bytes32[](1);

        bytes32 emptyRoot = sha256("");
        vm.prank(officer);
        registry.anchor(REPORT, HASH, emptyRoot, EVENTS, RULES, 0, 0, "");
        assertFalse(registry.verifyAlert(REPORT, emptyRoot, 0, none), "nothing belongs to a report with no alerts");
        assertFalse(registry.verifyAlert(REPORT, bytes32(0), 0, none));
        assertFalse(registry.verifyAlert(REPORT, emptyRoot, 0, one));
        assertFalse(registry.verifyAlert(REPORT, emptyRoot, type(uint256).max, none));

        bytes32 single = keccak256("single");
        bytes32 leaf = registry.leafHash("only");
        vm.prank(officer);
        registry.anchor(single, HASH, leaf, EVENTS, RULES, 1, 1, "");
        one[0] = leaf;
        assertTrue(registry.verifyAlert(single, leaf, 0, none));
        assertFalse(registry.verifyAlert(single, leaf, 1, none));
        assertFalse(registry.verifyAlert(single, leaf, 0, one), "the proof for a lone alert is empty");
        assertFalse(registry.verifyAlert(single, keccak256("x"), 0, none));
    }

    function test_InteriorNodeCannotPassAsAnAlert() public {
        // The left half of a four-leaf tree, offered as if it were an alert, with the right half as
        // its proof. The arithmetic works for a tree of two leaves, so the registry must not take
        // the size of the tree from the caller: it uses the alert count that was filed.
        bytes32[] memory leaves = _leaves(4, 7);
        bytes32 root = _root(leaves, 0, 4);
        bytes32 leftHalf = _root(leaves, 0, 2);
        bytes32[] memory proof = new bytes32[](1);
        proof[0] = _root(leaves, 2, 4);
        assertTrue(registry.verifyInclusion(leftHalf, 0, 2, proof, root), "true of a two-leaf tree");

        vm.prank(officer);
        registry.anchor(REPORT, HASH, root, EVENTS, RULES, 4, 3, "");
        for (uint256 i = 0; i < 4; ++i) {
            assertFalse(registry.verifyAlert(REPORT, leftHalf, i, proof));
        }
        assertTrue(registry.verifyAlert(REPORT, leaves[0], 0, _proof(leaves, 0, 0, 4)));
    }

    function test_VerifyInclusionRejectsDegenerateTrees() public view {
        bytes32[] memory none = new bytes32[](0);
        assertFalse(registry.verifyInclusion(bytes32(0), 0, 0, none, bytes32(0)));
        assertFalse(registry.verifyInclusion(sha256(""), 0, 0, none, sha256("")));

        uint256 max = type(uint256).max;
        bytes32[] memory long = new bytes32[](300);
        assertFalse(registry.verifyInclusion(HASH, max, max, none, HASH));
        assertFalse(registry.verifyInclusion(HASH, max - 1, max, none, HASH));
        assertFalse(registry.verifyInclusion(HASH, max - 1, max, long, HASH));
        assertFalse(registry.verifyInclusion(HASH, 0, max, long, HASH));
    }

    function testFuzz_InclusionProofs(uint8 sizeSeed, uint256 indexSeed, uint256 salt) public view {
        uint256 size = bound(uint256(sizeSeed), 1, 48);
        uint256 index = bound(indexSeed, 0, size - 1);
        bytes32[] memory leaves = _leaves(size, salt);
        bytes32 root = _root(leaves, 0, size);
        bytes32[] memory proof = _proof(leaves, index, 0, size);

        assertTrue(registry.verifyInclusion(leaves[index], index, size, proof, root));
        assertFalse(registry.verifyInclusion(keccak256(abi.encode(leaves[index])), index, size, proof, root));
        assertFalse(registry.verifyInclusion(leaves[index], size, size, proof, root));
        if (size > 1) {
            // The same leaf does not verify at a neighbouring position.
            uint256 other = index == 0 ? 1 : index - 1;
            assertFalse(registry.verifyInclusion(leaves[index], other, size, proof, root));
        }
        if (proof.length > 0) {
            proof[0] = keccak256(abi.encode(proof[0]));
            assertFalse(registry.verifyInclusion(leaves[index], index, size, proof, root));
        }
    }

    /// Soundness, exhaustively. Whatever the verifier accepts must be built from real nodes of the
    /// tree (or SHA-256 is broken), so it is enough to offer every node of the tree, leaf or not,
    /// with its true sibling path and with every shorter and longer one, at every index. Only a
    /// leaf, at its own index, with its whole path, may pass.
    function test_OnlyALeafAtItsOwnIndexVerifies() public view {
        for (uint256 size = 1; size <= 17; ++size) {
            bytes32[] memory leaves = _leaves(size, size * 31);
            _offerEveryNode(leaves, 0, size, size, _root(leaves, 0, size));
        }
    }

    function testFuzz_OnlyALeafAtItsOwnIndexVerifies(uint8 sizeSeed, uint256 salt) public view {
        uint256 size = bound(uint256(sizeSeed), 1, 24);
        bytes32[] memory leaves = _leaves(size, salt);
        _offerEveryNode(leaves, 0, size, size, _root(leaves, 0, size));
    }

    function _offerEveryNode(bytes32[] memory leaves, uint256 from, uint256 to, uint256 size, bytes32 root)
        internal
        view
    {
        bytes32 hash = _root(leaves, from, to);
        bytes32[] memory path = _copath(leaves, from, to, 0, size);
        bool isLeaf = to - from == 1;
        for (uint256 index = 0; index <= size + 1; ++index) {
            assertEq(registry.verifyInclusion(hash, index, size, path, root), isLeaf && index == from, "node at index");
            for (uint256 drop = 1; drop <= path.length; ++drop) {
                assertFalse(registry.verifyInclusion(hash, index, size, _slice(path, 0, path.length - drop), root));
                assertFalse(registry.verifyInclusion(hash, index, size, _slice(path, drop, path.length), root));
            }
            assertFalse(registry.verifyInclusion(hash, index, size, _append(path, bytes32(0)), root));
            assertFalse(registry.verifyInclusion(hash, index, size, _append(path, root), root));
            if (path.length > 0) {
                assertFalse(registry.verifyInclusion(hash, index, size, _append(path, path[path.length - 1]), root));
            }
        }
        if (!isLeaf) {
            uint256 k = _split(to - from);
            _offerEveryNode(leaves, from, from + k, size, root);
            _offerEveryNode(leaves, from + k, to, size, root);
        }
    }

    // -- administration --------------------------------------------------------------------------

    function test_TheDeployerOwnsTheRegistryAndMayFile() public {
        assertEq(registry.owner(), address(this));
        assertTrue(registry.isReporter(address(this)));
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 0, "");
        assertEq(registry.get(REPORT).reporter, address(this));
    }

    function test_OnlyOwnerAdministers() public {
        vm.startPrank(outsider);
        vm.expectRevert(ComplianceRegistry.NotOwner.selector);
        registry.addReporter(outsider);
        vm.expectRevert(ComplianceRegistry.NotOwner.selector);
        registry.removeReporter(officer);
        vm.expectRevert(ComplianceRegistry.NotOwner.selector);
        registry.transferOwnership(outsider);
        vm.stopPrank();
        assertTrue(registry.isReporter(officer));
        assertFalse(registry.isReporter(outsider));
        assertEq(registry.owner(), address(this));
    }

    function test_TransferOwnership() public {
        registry.transferOwnership(officer);
        assertEq(registry.owner(), officer);
        vm.expectRevert(ComplianceRegistry.NotOwner.selector);
        registry.addReporter(outsider);
        vm.prank(officer);
        registry.addReporter(outsider);
        assertTrue(registry.isReporter(outsider));
    }

    function test_OwningAndReportingAreSeparateRoles() public {
        registry.transferOwnership(outsider);
        // The new owner appoints reporters but is not one...
        assertFalse(registry.isReporter(outsider));
        vm.prank(outsider);
        vm.expectRevert(ComplianceRegistry.NotReporter.selector);
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 0, "");
        // ...and the old owner goes on filing until the new one says otherwise.
        assertTrue(registry.isReporter(address(this)));
        registry.anchor(REPORT, HASH, ROOT, EVENTS, RULES, 0, 0, "");
        vm.prank(outsider);
        registry.removeReporter(address(this));
        vm.expectRevert(ComplianceRegistry.NotReporter.selector);
        registry.anchor(keccak256("report-2"), HASH, ROOT, EVENTS, RULES, 0, 0, "");
    }

    function test_AdministrationIsLogged() public {
        vm.expectEmit(true, true, true, true);
        emit OwnershipTransferred(address(0), address(this));
        vm.expectEmit(true, true, true, true);
        emit ReporterAdded(address(this));
        ComplianceRegistry fresh = new ComplianceRegistry();

        vm.expectEmit(true, true, true, true, address(fresh));
        emit ReporterAdded(officer);
        fresh.addReporter(officer);

        vm.expectEmit(true, true, true, true, address(fresh));
        emit ReporterRemoved(officer);
        fresh.removeReporter(officer);

        vm.expectEmit(true, true, true, true, address(fresh));
        emit OwnershipTransferred(address(this), outsider);
        fresh.transferOwnership(outsider);
    }

    function test_RejectsZeroAddress() public {
        vm.expectRevert(ComplianceRegistry.ZeroAddress.selector);
        registry.addReporter(address(0));
        vm.expectRevert(ComplianceRegistry.ZeroAddress.selector);
        registry.transferOwnership(address(0));
    }

    // -- a reference Merkle tree (RFC 6962, section 2.1), written the obvious recursive way -------

    function _leaves(uint256 size, uint256 salt) internal pure returns (bytes32[] memory leaves) {
        leaves = new bytes32[](size);
        for (uint256 i = 0; i < size; ++i) {
            leaves[i] = sha256(abi.encodePacked(bytes1(0x00), salt, i));
        }
    }

    function _split(uint256 n) internal pure returns (uint256 k) {
        k = 1;
        while (k * 2 < n) k *= 2;
    }

    function _root(bytes32[] memory leaves, uint256 from, uint256 to) internal pure returns (bytes32) {
        uint256 n = to - from;
        if (n == 1) return leaves[from];
        uint256 k = _split(n);
        return sha256(abi.encodePacked(bytes1(0x01), _root(leaves, from, from + k), _root(leaves, from + k, to)));
    }

    /// The audit path of the leaf at `index` in the tree over leaves [from, to).
    function _proof(bytes32[] memory leaves, uint256 index, uint256 from, uint256 to)
        internal
        pure
        returns (bytes32[] memory)
    {
        return _copath(leaves, from + index, from + index + 1, from, to);
    }

    /// Sibling hashes from the subtree over leaves [a, b) up to the root of the tree over [from, to).
    function _copath(bytes32[] memory leaves, uint256 a, uint256 b, uint256 from, uint256 to)
        internal
        pure
        returns (bytes32[] memory)
    {
        if (a == from && b == to) return new bytes32[](0);
        uint256 k = _split(to - from);
        if (b <= from + k) {
            return _append(_copath(leaves, a, b, from, from + k), _root(leaves, from + k, to));
        }
        return _append(_copath(leaves, a, b, from + k, to), _root(leaves, from, from + k));
    }

    function _append(bytes32[] memory path, bytes32 extra) internal pure returns (bytes32[] memory out) {
        out = new bytes32[](path.length + 1);
        for (uint256 i = 0; i < path.length; ++i) {
            out[i] = path[i];
        }
        out[path.length] = extra;
    }

    function _slice(bytes32[] memory path, uint256 from, uint256 to) internal pure returns (bytes32[] memory out) {
        out = new bytes32[](to - from);
        for (uint256 i = from; i < to; ++i) {
            out[i - from] = path[i];
        }
    }
}
