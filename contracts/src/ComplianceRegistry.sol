// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

/// @title Compliance report registry
/// @notice A write-once register of compliance report fingerprints.
///
/// A report lives off chain. What is recorded here is enough to prove later that a given report
/// is the one that was filed: the SHA-256 of its bytes, the Merkle root of its alerts, and the
/// fingerprints of the evidence it was built from (the match's event stream and the rulebook in
/// force). An entry can never be changed or removed, and a report identifier can be used only
/// once, so whoever holds a report file can show it is the one that was filed under its identifier.
///
/// That alone would not stop a second, more convenient report about the same match being filed
/// under another identifier. So filings are also indexed by the event stream they are about:
/// whoever holds the exchange's own record of a match can ask for every report ever filed about it
/// (`reportsAbout`), and a quiet second filing is there for them to see.
///
/// Entries are chained: each stores a running hash over every field of every entry up to and
/// including itself, so a single 32-byte value commits to the whole history in order.
///
/// There are two roles and they are separate. The owner appoints and removes reporters; reporters
/// file. Handing over ownership neither lets the new owner file nor stops the old one: appoint and
/// remove them as reporters for that.
///
/// All hashes are SHA-256 rather than Keccak, so that every one of them can be checked with
/// ordinary tools (`shasum -a 256 report.json`) by someone who has never heard of Ethereum.
contract ComplianceRegistry {
    struct Anchor {
        bytes32 reportHash; // SHA-256 of the report file
        bytes32 alertsRoot; // Merkle root of the report's alerts (RFC 6962 construction, SHA-256)
        bytes32 eventsHash; // SHA-256 of the match event stream the report was built from
        bytes32 rulebookHash; // SHA-256 of the rulebook the match was judged against
        bytes32 chainHead; // running hash over every entry up to and including this one
        uint64 anchoredAt; // block timestamp
        uint64 sequence; // position in the registry, starting at 1; zero marks an identifier never used
        uint32 alertCount; // number of alerts, which is the number of leaves under alertsRoot
        uint8 worstSeverity; // 0 none, 1 info, 2 low, 3 medium, 4 high, 5 critical
        address reporter;
    }

    uint8 public constant MAX_SEVERITY = 5;

    address public owner;
    mapping(address => bool) public isReporter;

    /// @notice Running hash over every anchor filed so far; zero while the registry is empty.
    bytes32 public head;

    mapping(bytes32 => Anchor) private _anchors;
    bytes32[] private _reportIds;
    mapping(bytes32 => bytes32[]) private _reportsAbout; // events hash => the reports filed about it

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

    error NotOwner();
    error NotReporter();
    error AlreadyAnchored(bytes32 reportId);
    error UnknownReport(bytes32 reportId);
    error NoSuchEntry(uint256 sequence);
    error EmptyFingerprint();
    error InvalidSeverity(uint8 severity);
    error ZeroAddress();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyReporter() {
        if (!isReporter[msg.sender]) revert NotReporter();
        _;
    }

    constructor() {
        owner = msg.sender;
        isReporter[msg.sender] = true;
        emit OwnershipTransferred(address(0), msg.sender);
        emit ReporterAdded(msg.sender);
    }

    // -- filing ----------------------------------------------------------------------------------

    /// @notice File a report. Reverts if `reportId` has been used before.
    /// @param reportId SHA-256 of the report's identifier.
    /// @param uri Where the report itself can be fetched. Logged, not stored.
    /// @return sequence The report's position in the registry, starting at 1.
    function anchor(
        bytes32 reportId,
        bytes32 reportHash,
        bytes32 alertsRoot,
        bytes32 eventsHash,
        bytes32 rulebookHash,
        uint32 alertCount,
        uint8 worstSeverity,
        string calldata uri
    ) external onlyReporter returns (uint64 sequence) {
        if (reportHash == bytes32(0)) revert EmptyFingerprint();
        if (worstSeverity > MAX_SEVERITY) revert InvalidSeverity(worstSeverity);
        if (_anchors[reportId].sequence != 0) revert AlreadyAnchored(reportId);

        // forge-lint: disable-next-line(unsafe-typecast)
        sequence = uint64(_reportIds.length + 1); // 2^64 filings is not a reachable number
        // forge-lint: disable-next-line(unsafe-typecast)
        uint64 anchoredAt = uint64(block.timestamp); // seconds since 1970 fit in 64 bits for 584 billion years
        // Everything the entry records goes into the running hash, the alert count included: it is
        // what stops an interior node of the Merkle tree from being passed off as an alert.
        bytes32 chainHead = sha256(
            abi.encodePacked(
                head,
                reportId,
                reportHash,
                alertsRoot,
                eventsHash,
                rulebookHash,
                alertCount,
                worstSeverity,
                msg.sender,
                anchoredAt
            )
        );
        _anchors[reportId] = Anchor({
            reportHash: reportHash,
            alertsRoot: alertsRoot,
            eventsHash: eventsHash,
            rulebookHash: rulebookHash,
            chainHead: chainHead,
            anchoredAt: anchoredAt,
            sequence: sequence,
            alertCount: alertCount,
            worstSeverity: worstSeverity,
            reporter: msg.sender
        });
        _reportIds.push(reportId);
        _reportsAbout[eventsHash].push(reportId);
        head = chainHead;
        emit ReportAnchored(reportId, sequence, msg.sender, reportHash, alertsRoot, chainHead, uri);
    }

    // -- reading ---------------------------------------------------------------------------------

    function count() external view returns (uint256) {
        return _reportIds.length;
    }

    function isAnchored(bytes32 reportId) public view returns (bool) {
        return _anchors[reportId].sequence != 0;
    }

    function get(bytes32 reportId) external view returns (Anchor memory) {
        if (!isAnchored(reportId)) revert UnknownReport(reportId);
        return _anchors[reportId];
    }

    /// @param sequence Position in the registry, starting at 1.
    function reportIdAt(uint256 sequence) external view returns (bytes32) {
        if (sequence == 0 || sequence > _reportIds.length) revert NoSuchEntry(sequence);
        return _reportIds[sequence - 1];
    }

    /// @notice Every report filed about the event stream with this fingerprint, in filing order.
    /// @return Their identifiers; empty if none has been filed.
    function reportsAbout(bytes32 eventsHash) external view returns (bytes32[] memory) {
        return _reportsAbout[eventsHash];
    }

    // -- verification ----------------------------------------------------------------------------

    /// @notice Is `reportHash` the fingerprint that was filed under `reportId`?
    function verifyReport(bytes32 reportId, bytes32 reportHash) external view returns (bool) {
        return isAnchored(reportId) && _anchors[reportId].reportHash == reportHash;
    }

    /// @notice Does the alert with leaf hash `leaf` belong to the report filed under `reportId`?
    /// @param index Position of the alert in the report, starting at 0.
    /// @param proof The alert's audit path: sibling hashes from the leaf up to the root.
    function verifyAlert(bytes32 reportId, bytes32 leaf, uint256 index, bytes32[] calldata proof)
        external
        view
        returns (bool)
    {
        if (!isAnchored(reportId)) return false;
        Anchor storage filed = _anchors[reportId];
        return verifyInclusion(leaf, index, filed.alertCount, proof, filed.alertsRoot);
    }

    /// @notice Hash of a Merkle leaf: SHA-256 of a zero byte followed by the data (RFC 6962).
    function leafHash(bytes calldata data) external pure returns (bytes32) {
        return sha256(abi.encodePacked(bytes1(0x00), data));
    }

    /// @notice Check a Merkle audit path, as specified in RFC 9162 section 2.1.3.2.
    function verifyInclusion(bytes32 leaf, uint256 index, uint256 size, bytes32[] calldata proof, bytes32 root)
        public
        pure
        returns (bool)
    {
        if (index >= size) return false;
        uint256 node = index;
        uint256 last = size - 1;
        bytes32 current = leaf;
        for (uint256 i = 0; i < proof.length; ++i) {
            if (last == 0) return false;
            if (node & 1 == 1 || node == last) {
                current = sha256(abi.encodePacked(bytes1(0x01), proof[i], current));
                if (node & 1 == 0) {
                    while (node != 0 && node & 1 == 0) {
                        node >>= 1;
                        last >>= 1;
                    }
                }
            } else {
                current = sha256(abi.encodePacked(bytes1(0x01), current, proof[i]));
            }
            node >>= 1;
            last >>= 1;
        }
        return last == 0 && current == root;
    }

    // -- administration --------------------------------------------------------------------------

    // The lint below does not recognise an event as covering a write to a mapping; both of these
    // functions emit one naming the account.

    function addReporter(address account) external onlyOwner {
        if (account == address(0)) revert ZeroAddress();
        // forge-lint: disable-next-line(missing-events-access-control)
        isReporter[account] = true;
        emit ReporterAdded(account);
    }

    function removeReporter(address account) external onlyOwner {
        // forge-lint: disable-next-line(missing-events-access-control)
        isReporter[account] = false;
        emit ReporterRemoved(account);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        address previousOwner = owner;
        owner = newOwner;
        emit OwnershipTransferred(previousOwner, newOwner);
    }
}
