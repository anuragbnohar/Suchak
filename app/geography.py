"""RBI regional office jurisdictions and the places that define them.

The RD View's in-region tab needs to know, for news about an entity
headquartered elsewhere, whether the story happened in this office's
region. Jurisdiction is by state, so each office maps to its state(s)
and each state to its districts and major cities -- the names a
journalist actually writes. Maharashtra is the special case: the Nagpur
office covers the Vidarbha districts and Mumbai/Belapur the rest, so
those offices carve the state between them.

Static on purpose: office jurisdictions are facts, not preferences. A
district missing here costs one missed match, never a crash -- and the
same name in two states (Aurangabad, Bilaspur, Hamirpur) simply counts
for both regions, because without more context that is the truth.
"""

# Office -> states (or carved parts of one, see the Maharashtra split).
OFFICE_STATES = {
    "Agartala": ["Tripura"],
    "Ahmedabad": ["Gujarat", "Dadra and Daman"],
    "Aizawl": ["Mizoram"],
    "Belapur": ["Maharashtra (excluding Vidarbha)"],
    "Bengaluru": ["Karnataka"],
    "Bhopal": ["Madhya Pradesh"],
    "Bhubaneswar": ["Odisha"],
    "Chandigarh": ["Punjab", "Chandigarh"],
    "Chennai": ["Tamil Nadu", "Puducherry"],
    "Dehradun": ["Uttarakhand"],
    "Gangtok": ["Sikkim"],
    "Guwahati": ["Assam"],
    "Hyderabad": ["Telangana", "Andhra Pradesh"],
    "Imphal": ["Manipur"],
    "Itanagar": ["Arunachal Pradesh"],
    "Jaipur": ["Rajasthan"],
    "Jammu": ["Jammu and Kashmir", "Ladakh"],
    "Kanpur": ["Uttar Pradesh"],
    "Kohima": ["Nagaland"],
    "Kolkata": ["West Bengal", "Andaman and Nicobar"],
    "Lucknow": ["Uttar Pradesh"],
    "Mumbai": ["Maharashtra (excluding Vidarbha)"],
    "Nagpur": ["Maharashtra (Vidarbha)"],
    "New Delhi": ["Delhi", "Haryana"],
    "Panaji": ["Goa"],
    "Patna": ["Bihar"],
    "Raipur": ["Chhattisgarh"],
    "Ranchi": ["Jharkhand"],
    "Shillong": ["Meghalaya"],
    "Shimla": ["Himachal Pradesh"],
    "Srinagar": ["Jammu and Kashmir", "Ladakh"],
    "Thiruvananthapuram": ["Kerala", "Lakshadweep"],
}

# Alternate spellings a paper might use for the state itself.
STATE_NAMES = {
    "Odisha": ["Odisha", "Orissa"],
    "Puducherry": ["Puducherry", "Pondicherry"],
    "Jammu and Kashmir": ["Jammu", "Kashmir"],
    "Andaman and Nicobar": ["Andaman", "Nicobar"],
    "Dadra and Daman": ["Daman", "Diu", "Silvassa", "Dadra"],
    "Maharashtra (Vidarbha)": ["Vidarbha"],
    "Maharashtra (excluding Vidarbha)": ["Maharashtra"],
}

_VIDARBHA = ["Nagpur", "Wardha", "Bhandara", "Gondia", "Chandrapur",
             "Gadchiroli", "Amravati", "Akola", "Washim", "Buldhana",
             "Yavatmal"]

_MAHA_REST = ["Mumbai", "Thane", "Palghar", "Raigad", "Ratnagiri",
              "Sindhudurg", "Pune", "Satara", "Sangli", "Solapur",
              "Kolhapur", "Nashik", "Ahmednagar", "Ahilyanagar", "Dhule",
              "Nandurbar", "Jalgaon", "Aurangabad",
              "Chhatrapati Sambhajinagar", "Sambhajinagar", "Jalna",
              "Beed", "Latur", "Osmanabad", "Dharashiv", "Nanded",
              "Parbhani", "Hingoli", "Navi Mumbai"]

STATE_DISTRICTS = {
    "Maharashtra (Vidarbha)": _VIDARBHA,
    "Maharashtra (excluding Vidarbha)": _MAHA_REST,
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot", "Bhavnagar",
        "Jamnagar", "Junagadh", "Gandhinagar", "Kutch", "Bhuj", "Mehsana",
        "Patan", "Banaskantha", "Palanpur", "Sabarkantha", "Himmatnagar",
        "Aravalli", "Anand", "Kheda", "Nadiad", "Panchmahal", "Godhra",
        "Dahod", "Mahisagar", "Bharuch", "Narmada", "Navsari", "Valsad",
        "Vapi", "Tapi", "Dang", "Surendranagar", "Morbi", "Botad",
        "Amreli", "Gir Somnath", "Porbandar", "Dwarka", "Chhota Udepur"],
    "Uttar Pradesh": ["Lucknow", "Kanpur", "Agra", "Varanasi", "Prayagraj",
        "Allahabad", "Meerut", "Ghaziabad", "Noida", "Aligarh", "Bareilly",
        "Moradabad", "Saharanpur", "Gorakhpur", "Jhansi", "Mathura",
        "Firozabad", "Ayodhya", "Faizabad", "Rampur", "Shahjahanpur",
        "Muzaffarnagar", "Bulandshahr", "Etawah", "Mainpuri", "Etah",
        "Badaun", "Pilibhit", "Sitapur", "Hardoi", "Unnao", "Rae Bareli",
        "Fatehpur", "Banda", "Hamirpur", "Mahoba", "Chitrakoot", "Jalaun",
        "Lalitpur", "Kannauj", "Farrukhabad", "Auraiya", "Amethi",
        "Sultanpur", "Pratapgarh", "Kaushambi", "Mirzapur", "Sonbhadra",
        "Bhadohi", "Chandauli", "Ghazipur", "Jaunpur", "Azamgarh", "Mau",
        "Ballia", "Deoria", "Kushinagar", "Maharajganj", "Basti",
        "Siddharthnagar", "Gonda", "Balrampur", "Bahraich", "Shravasti",
        "Ambedkar Nagar", "Barabanki", "Lakhimpur Kheri", "Hathras",
        "Kasganj", "Sambhal", "Amroha", "Bijnor", "Shamli", "Baghpat",
        "Hapur"],
    "Karnataka": ["Bengaluru", "Bangalore", "Mysuru", "Mysore",
        "Mangaluru", "Mangalore", "Hubballi", "Hubli", "Dharwad",
        "Belagavi", "Belgaum", "Ballari", "Bellary", "Vijayapura",
        "Kalaburagi", "Gulbarga", "Davangere", "Shivamogga", "Shimoga",
        "Tumakuru", "Tumkur", "Udupi", "Hassan", "Mandya",
        "Chikkamagaluru", "Kolar", "Chikkaballapur", "Ramanagara",
        "Chitradurga", "Haveri", "Gadag", "Koppal", "Raichur", "Bidar",
        "Yadgir", "Bagalkot", "Uttara Kannada", "Karwar",
        "Dakshina Kannada", "Kodagu", "Chamarajanagar"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Tiruchirappalli",
        "Trichy", "Salem", "Tirunelveli", "Erode", "Vellore",
        "Thoothukudi", "Tuticorin", "Thanjavur", "Dindigul",
        "Kanyakumari", "Karur", "Namakkal", "Krishnagiri", "Dharmapuri",
        "Villupuram", "Cuddalore", "Chengalpattu", "Kancheepuram",
        "Tiruvallur", "Tiruvannamalai", "Ranipet", "Tirupattur",
        "Tenkasi", "Virudhunagar", "Ramanathapuram", "Sivaganga",
        "Pudukkottai", "Ariyalur", "Perambalur", "Nagapattinam",
        "Mayiladuthurai", "Tiruvarur", "Nilgiris", "Ooty", "Theni",
        "Tirupur", "Kallakurichi"],
    "Telangana": ["Hyderabad", "Secunderabad", "Warangal", "Nizamabad",
        "Karimnagar", "Khammam", "Mahbubnagar", "Nalgonda", "Adilabad",
        "Medak", "Rangareddy", "Siddipet", "Suryapet", "Jagtial",
        "Mancherial", "Kamareddy", "Vikarabad", "Sangareddy", "Medchal",
        "Nagarkurnool", "Wanaparthy", "Gadwal", "Narayanpet",
        "Mahabubabad", "Bhupalpally", "Mulugu", "Asifabad", "Nirmal",
        "Peddapalli", "Sircilla", "Jangaon", "Bhongir"],
    "Andhra Pradesh": ["Visakhapatnam", "Vizag", "Vijayawada", "Guntur",
        "Nellore", "Kurnool", "Tirupati", "Kadapa", "Anantapur",
        "Chittoor", "Rajahmundry", "Rajamahendravaram", "Kakinada",
        "Eluru", "Ongole", "Srikakulam", "Vizianagaram", "Machilipatnam",
        "Krishna", "East Godavari", "West Godavari", "Prakasam",
        "Annamayya", "Bapatla", "Palnadu", "Anakapalli", "Parvathipuram",
        "Nandyal"],
    "Kerala": ["Thiruvananthapuram", "Kollam", "Pathanamthitta",
        "Alappuzha", "Kottayam", "Idukki", "Ernakulam", "Kochi",
        "Thrissur", "Palakkad", "Malappuram", "Kozhikode", "Wayanad",
        "Kannur", "Kasaragod"],
    "West Bengal": ["Kolkata", "Howrah", "Hooghly", "Asansol", "Durgapur",
        "Siliguri", "Darjeeling", "Kalimpong", "Jalpaiguri",
        "Alipurduar", "Cooch Behar", "Malda", "Murshidabad",
        "Berhampore", "Nadia", "Krishnanagar", "Barasat", "Bardhaman",
        "Burdwan", "Birbhum", "Bankura", "Purulia", "Midnapore",
        "Paschim Medinipur", "Purba Medinipur", "Haldia", "Jhargram",
        "Raiganj", "Balurghat", "Parganas"],
    "Odisha": ["Bhubaneswar", "Cuttack", "Rourkela", "Sambalpur",
        "Berhampur", "Brahmapur", "Puri", "Balasore", "Baleswar",
        "Bhadrak", "Angul", "Dhenkanal", "Kendrapara", "Jagatsinghpur",
        "Jajpur", "Keonjhar", "Mayurbhanj", "Baripada", "Koraput",
        "Rayagada", "Nabarangpur", "Malkangiri", "Kalahandi",
        "Bhawanipatna", "Bolangir", "Balangir", "Sonepur", "Bargarh",
        "Jharsuguda", "Sundargarh", "Deogarh", "Boudh", "Kandhamal",
        "Phulbani", "Gajapati", "Ganjam", "Nayagarh", "Khordha",
        "Nuapada"],
    "Madhya Pradesh": ["Bhopal", "Indore", "Gwalior", "Jabalpur",
        "Ujjain", "Sagar", "Rewa", "Satna", "Ratlam", "Dewas", "Katni",
        "Chhindwara", "Vidisha", "Sehore", "Raisen", "Hoshangabad",
        "Narmadapuram", "Betul", "Harda", "Khandwa", "Khargone",
        "Burhanpur", "Barwani", "Dhar", "Jhabua", "Alirajpur",
        "Mandsaur", "Neemuch", "Shajapur", "Agar Malwa", "Rajgarh",
        "Guna", "Ashoknagar", "Shivpuri", "Datia", "Bhind", "Morena",
        "Sheopur", "Tikamgarh", "Chhatarpur", "Panna", "Damoh",
        "Narsinghpur", "Seoni", "Mandla", "Dindori", "Balaghat",
        "Shahdol", "Umaria", "Anuppur", "Sidhi", "Singrauli", "Maihar",
        "Niwari"],
    "Chhattisgarh": ["Raipur", "Bilaspur", "Durg", "Bhilai", "Korba",
        "Rajnandgaon", "Raigarh", "Jagdalpur", "Bastar", "Ambikapur",
        "Surguja", "Dhamtari", "Mahasamund", "Kanker", "Kawardha",
        "Janjgir", "Champa", "Mungeli", "Balod", "Baloda Bazar",
        "Bemetara", "Gariaband", "Kondagaon", "Sukma", "Dantewada",
        "Narayanpur", "Jashpur", "Koriya", "Surajpur"],
    "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur", "Kota", "Ajmer",
        "Bikaner", "Alwar", "Bharatpur", "Sikar", "Jhunjhunu", "Churu",
        "Ganganagar", "Hanumangarh", "Nagaur", "Pali", "Barmer",
        "Jaisalmer", "Jalore", "Sirohi", "Bhilwara", "Chittorgarh",
        "Rajsamand", "Dungarpur", "Banswara", "Baran", "Bundi",
        "Jhalawar", "Sawai Madhopur", "Karauli", "Dholpur", "Dausa",
        "Tonk"],
    "Punjab": ["Ludhiana", "Amritsar", "Jalandhar", "Patiala",
        "Bathinda", "Mohali", "Hoshiarpur", "Gurdaspur", "Pathankot",
        "Ferozepur", "Firozpur", "Fazilka", "Faridkot", "Muktsar",
        "Moga", "Barnala", "Sangrur", "Malerkotla", "Mansa",
        "Kapurthala", "Nawanshahr", "Rupnagar", "Ropar",
        "Fatehgarh Sahib", "Tarn Taran"],
    "Haryana": ["Gurugram", "Gurgaon", "Faridabad", "Panipat", "Ambala",
        "Yamunanagar", "Rohtak", "Hisar", "Karnal", "Sonipat",
        "Panchkula", "Bhiwani", "Sirsa", "Jind", "Kaithal",
        "Kurukshetra", "Fatehabad", "Rewari", "Mahendragarh", "Narnaul",
        "Jhajjar", "Palwal", "Nuh", "Mewat", "Charkhi Dadri"],
    "Himachal Pradesh": ["Shimla", "Mandi", "Kangra", "Dharamshala",
        "Solan", "Kullu", "Manali", "Hamirpur", "Una", "Bilaspur",
        "Chamba", "Sirmaur", "Nahan", "Kinnaur", "Lahaul", "Spiti",
        "Keylong"],
    "Uttarakhand": ["Dehradun", "Haridwar", "Rishikesh", "Roorkee",
        "Nainital", "Haldwani", "Udham Singh Nagar", "Rudrapur",
        "Almora", "Pithoragarh", "Champawat", "Bageshwar", "Chamoli",
        "Rudraprayag", "Tehri", "Pauri", "Uttarkashi"],
    "Delhi": ["Delhi", "New Delhi"],
    "Bihar": ["Patna", "Gaya", "Bhagalpur", "Muzaffarpur", "Darbhanga",
        "Purnia", "Ara", "Arrah", "Bhojpur", "Begusarai", "Katihar",
        "Munger", "Chhapra", "Saran", "Samastipur", "Motihari",
        "Champaran", "Bettiah", "Siwan", "Gopalganj", "Vaishali",
        "Hajipur", "Sitamarhi", "Sheohar", "Madhubani", "Supaul",
        "Araria", "Kishanganj", "Madhepura", "Saharsa", "Khagaria",
        "Nalanda", "Biharsharif", "Nawada", "Jehanabad", "Arwal",
        "Aurangabad", "Jamui", "Lakhisarai", "Sheikhpura", "Banka",
        "Rohtas", "Sasaram", "Buxar", "Kaimur"],
    "Jharkhand": ["Ranchi", "Jamshedpur", "Dhanbad", "Bokaro", "Deoghar",
        "Hazaribagh", "Giridih", "Dumka", "Palamu", "Daltonganj",
        "Chatra", "Koderma", "Jamtara", "Sahibganj", "Pakur", "Godda",
        "Lohardaga", "Gumla", "Simdega", "Khunti", "Saraikela",
        "Chaibasa", "Singhbhum", "Ramgarh", "Latehar", "Garhwa"],
    "Assam": ["Guwahati", "Dispur", "Dibrugarh", "Silchar", "Jorhat",
        "Tezpur", "Nagaon", "Tinsukia", "Bongaigaon", "Dhubri",
        "Goalpara", "Barpeta", "Nalbari", "Kamrup", "Darrang",
        "Mangaldoi", "Sonitpur", "Lakhimpur", "Dhemaji", "Majuli",
        "Sivasagar", "Golaghat", "Karbi Anglong", "Diphu", "Dima Hasao",
        "Haflong", "Cachar", "Karimganj", "Hailakandi", "Morigaon",
        "Baksa", "Chirang", "Kokrajhar", "Udalguri", "Biswanath",
        "Hojai", "Charaideo"],
    "Tripura": ["Agartala", "Dharmanagar", "Kailashahar", "Ambassa",
        "Khowai", "Belonia", "Sabroom", "Sonamura", "Bishalgarh"],
    "Manipur": ["Imphal", "Churachandpur", "Thoubal", "Bishnupur",
        "Ukhrul", "Senapati", "Tamenglong", "Chandel", "Jiribam",
        "Kangpokpi", "Kakching", "Tengnoupal", "Kamjong", "Noney",
        "Pherzawl"],
    "Meghalaya": ["Shillong", "Tura", "Jowai", "Nongpoh", "Nongstoin",
        "Williamnagar", "Baghmara", "Resubelpara", "Mairang",
        "Khliehriat", "Ampati"],
    "Mizoram": ["Aizawl", "Lunglei", "Champhai", "Serchhip", "Kolasib",
        "Mamit", "Lawngtlai", "Saiha", "Siaha", "Khawzawl", "Saitual",
        "Hnahthial"],
    "Nagaland": ["Kohima", "Dimapur", "Mokokchung", "Tuensang", "Mon",
        "Wokha", "Zunheboto", "Phek", "Kiphire", "Longleng", "Peren",
        "Noklak", "Chumoukedima", "Niuland", "Tseminyu", "Shamator"],
    "Arunachal Pradesh": ["Itanagar", "Naharlagun", "Tawang", "Bomdila",
        "Ziro", "Pasighat", "Tezu", "Roing", "Aalo", "Along", "Daporijo",
        "Seppa", "Changlang", "Khonsa", "Longding", "Anini", "Yingkiong",
        "Basar", "Namsai"],
    "Sikkim": ["Gangtok", "Namchi", "Gyalshing", "Mangan", "Pakyong",
        "Soreng"],
    "Goa": ["Panaji", "Margao", "Vasco", "Mapusa", "Ponda", "North Goa",
        "South Goa"],
    "Jammu and Kashmir": ["Srinagar", "Jammu", "Anantnag", "Baramulla",
        "Budgam", "Pulwama", "Kupwara", "Bandipora", "Ganderbal",
        "Kulgam", "Shopian", "Udhampur", "Kathua", "Doda", "Kishtwar",
        "Ramban", "Reasi", "Rajouri", "Poonch", "Samba"],
    "Ladakh": ["Leh", "Kargil"],
    "Puducherry": ["Puducherry", "Pondicherry", "Karaikal", "Mahe",
        "Yanam"],
    "Chandigarh": ["Chandigarh"],
    "Andaman and Nicobar": ["Port Blair", "Andaman", "Nicobar"],
    "Lakshadweep": ["Kavaratti", "Lakshadweep"],
    "Dadra and Daman": ["Daman", "Diu", "Silvassa"],
}


def office_places(office: str) -> list[str]:
    """Every place name that counts as this office's region: the state
    name(s) and their districts/major cities. An office this module does
    not know matches on its own name alone, so a custom office still
    works, just narrowly."""
    states = OFFICE_STATES.get(office)
    if not states:
        return [office]
    seen: set[str] = set()
    places: list[str] = []
    for st in states:
        for term in STATE_NAMES.get(st, [st]) + STATE_DISTRICTS.get(st, []):
            if term.lower() not in seen:
                seen.add(term.lower())
                places.append(term)
    return places


def describe(office: str) -> str:
    """One line saying what region an office covers, for the RD View."""
    states = OFFICE_STATES.get(office)
    if not states:
        return office
    pretty = [s.replace(" (excluding Vidarbha)", " excluding Vidarbha")
               .replace(" (Vidarbha)", " — Vidarbha districts")
              for s in states]
    return ", ".join(pretty)
